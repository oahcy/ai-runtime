import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .engine import Runtime


class CreateSession(BaseModel):
    cwd: str


class Execute(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=262144)
    timeout: float = Field(default=30, ge=0.1, le=3600)


def create_app(root: Path, token: str):
    if not token:
        raise ValueError('an authentication token is required')

    @asynccontextmanager
    async def lifespan(app):
        runtime = Runtime(root)
        app.state.runtime = runtime
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(title='Local AI Runtime', version='0.1.0', lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if not credentials or not secrets.compare_digest(credentials.credentials, token):
            raise HTTPException(401, 'Bearer token required')

    secured = [Depends(authorize)]

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def conflict(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.get('/', include_in_schema=False)
    async def index():
        return RedirectResponse('/docs')

    @app.get('/favicon.ico', include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'mode': 'trusted-local', 'version': '0.1.0'}

    @app.post('/sessions', dependencies=secured, status_code=201)
    async def create(body: CreateSession):
        return await app.state.runtime.create(body.cwd)

    @app.get('/sessions', dependencies=secured)
    async def sessions():
        return app.state.runtime.store.all('SELECT * FROM sessions ORDER BY created_at')

    @app.get('/sessions/{sid}', dependencies=secured)
    async def session(sid: str):
        return app.state.runtime.session(sid)

    @app.delete('/sessions/{sid}', dependencies=secured)
    async def close(sid: str):
        async with app.state.runtime.lifecycle_lock:
            await app.state.runtime.close_session(sid)
        return app.state.runtime.session(sid)

    @app.post('/sessions/{sid}/restart', dependencies=secured)
    async def restart(sid: str):
        return await app.state.runtime.restart(sid)

    @app.post('/sessions/{sid}/executions', dependencies=secured, status_code=202)
    async def execute(sid: str, body: Execute):
        return app.state.runtime.submit(sid, body.request_id, body.code, body.timeout)

    @app.get('/executions/{eid}', dependencies=secured)
    async def execution(eid: str):
        return app.state.runtime.execution(eid)

    @app.post('/executions/{eid}/interrupt', dependencies=secured)
    async def interrupt(eid: str):
        return await app.state.runtime.interrupt(eid)

    @app.get('/sessions/{sid}/events', dependencies=secured)
    async def events(sid: str, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
        runtime = app.state.runtime
        runtime.session(sid)
        rows = runtime.store.events(sid, after, limit)
        return {'events': rows, 'cursor': rows[-1]['seq'] if rows else after}

    @app.websocket('/sessions/{sid}/events/ws')
    async def event_socket(websocket: WebSocket, sid: str, after: int = Query(0, ge=0)):
        header = websocket.headers.get('authorization', '')
        if not secrets.compare_digest(header, 'Bearer ' + token):
            await websocket.close(code=1008)
            return
        runtime = app.state.runtime
        try:
            runtime.session(sid)
        except KeyError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        # Read acknowledgements/pings to detect a quiet client's disconnection.
        async def receive():
            while True:
                await websocket.receive_text()
        reader = asyncio.create_task(receive())
        try:
            while not reader.done():
                rows = runtime.store.events(sid, after)
                for row in rows:
                    await websocket.send_json(row)
                    after = row['seq']
                if not rows:
                    await asyncio.sleep(0.1)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    @app.get('/artifacts/{aid}', dependencies=secured)
    async def artifact(aid: str):
        runtime = app.state.runtime
        row = runtime.store.one('SELECT * FROM artifacts WHERE id=?', (aid,))
        if not row:
            raise HTTPException(404, 'artifact not found')
        path = Path(row['path']).resolve()
        allowed = (runtime.store.root / 'sessions' / row['session_id'] / 'artifacts').resolve()
        if not path.is_relative_to(allowed) or not path.is_file():
            raise HTTPException(404, 'artifact unavailable')
        return FileResponse(path, media_type=row['mime'], filename=path.name)

    @app.get('/sessions/{sid}/checkpoint', dependencies=secured)
    async def checkpoint(sid: str):
        runtime = app.state.runtime
        runtime.session(sid)
        path = runtime.store.root / 'sessions' / sid / 'checkpoint.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    return app
