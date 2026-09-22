"""Main-service in-memory preview hosting and session-scoped producer channels."""
import asyncio
import json
import re
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from .preview import render_html, PAGE_HEADERS

ID = re.compile(r'^[0-9a-f]{32}$')


def snapshot(value):
    if (not isinstance(value, dict) or type(value.get('revision')) is not int or value['revision'] < 0
            or not isinstance(value.get('state'), dict) or not isinstance(value.get('history'), list)):
        raise ValueError('Invalid preview snapshot')
    if len(json.dumps(value, allow_nan=False).encode()) > 4 * 1024 * 1024:
        raise ValueError('Preview snapshot exceeds 4 MiB')
    return value


class PreviewHub:
    def __init__(self):
        self.sessions = {}
        self.views = {}

    def attach(self, sid):
        self.detach(sid)
        key = secrets.token_urlsafe(32)
        self.sessions[sid] = key
        return key

    def detach(self, sid):
        self.sessions.pop(sid, None)
        for pid, row in list(self.views.items()):
            if row['sid'] == sid:
                self.views.pop(pid, None)

    def list(self, base_url, session_id=None):
        return [
            dict(id=pid, session_id=row['sid'], name=row['name'], running=True,
                 url=f"{base_url.rstrip('/')}/previews/{pid}/#{row['token']}",
                 revision=row['snapshot']['revision'],
                 created_at=row['created_at'], updated_at=row['updated_at'])
            for pid, row in self.views.items()
            if session_id is None or row['sid'] == session_id
        ]

    def mount(self, app):
        @app.websocket('/_internal/previews/{sid}/{pid}')
        async def producer(ws: WebSocket, sid: str, pid: str):
            key = self.sessions.get(sid)
            if (not ID.fullmatch(pid) or not key or
                    not secrets.compare_digest(ws.headers.get('authorization', ''), 'Bearer ' + key)):
                await ws.close(code=1008)
                return
            await ws.accept()
            row = None
            try:
                raw = await asyncio.wait_for(ws.receive_text(), 5)
                if len(raw.encode()) > 8 * 1024 * 1024:
                    raise ValueError('Registration too large')
                registration = json.loads(raw)
                html, token = registration.get('html'), registration.get('token')
                name = registration.get('name', 'preview')
                if not isinstance(name, str) or not name.strip() or len(name) > 128:
                    raise ValueError('Invalid preview name')
                if (not isinstance(html, str) or len(html.encode()) > 2 * 1024 * 1024
                        or not isinstance(token, str) or not 32 <= len(token) <= 128):
                    raise ValueError('Invalid registration')
                if self.sessions.get(sid) != key or pid in self.views:
                    raise ValueError('Invalid session or duplicate preview')
                if sum(v['sid'] == sid for v in self.views.values()) >= 16:
                    raise ValueError('Maximum 16 previews per session')
                now = datetime.now(timezone.utc).isoformat()
                row = dict(sid=sid, html=html, token=token, name=name, created_at=now, updated_at=now,
                           snapshot=snapshot(registration.get('snapshot')))
                self.views[pid] = row
                await ws.send_json({'ok': True})
                while self.sessions.get(sid) == key and self.views.get(pid) is row:
                    raw = await asyncio.wait_for(ws.receive_text(), 3)
                    if len(raw.encode()) > 4 * 1024 * 1024:
                        raise ValueError('Frame too large')
                    message = json.loads(raw)
                    if message.get('type') == 'stop':
                        self.views.pop(pid, None)
                        await ws.send_json({'stopped': True})
                        return
                    if message.get('type') == 'state':
                        value = snapshot(message.get('snapshot'))
                        if value['revision'] > row['snapshot']['revision']:
                            row['snapshot'] = value
                            row['updated_at'] = datetime.now(timezone.utc).isoformat()
                    elif message.get('type') != 'ping':
                        raise ValueError('Unknown producer message')
                    await ws.send_json({'ok': True})
            except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, RuntimeError, TypeError, AttributeError):
                pass
            finally:
                if row is not None and self.views.get(pid) is row:
                    self.views.pop(pid, None)
                try:
                    await ws.close()
                except (RuntimeError, WebSocketDisconnect):
                    pass

        @app.get('/previews/{pid}', include_in_schema=False)
        async def canonical(pid: str):
            if pid not in self.views:
                raise HTTPException(404, 'Preview unavailable')
            return RedirectResponse(f'/previews/{pid}/')

        @app.get('/previews/{pid}/', include_in_schema=False)
        async def page(pid: str):
            row = self.views.get(pid)
            if row is None:
                raise HTTPException(404, 'Preview unavailable')
            return HTMLResponse(render_html(row['html']), headers=PAGE_HEADERS)

        @app.websocket('/previews/{pid}/ws')
        async def viewer(ws: WebSocket, pid: str):
            row = self.views.get(pid)
            expected_origin = str(ws.url).split('/previews/')[0].replace('ws://', 'http://').replace('wss://', 'https://')
            if row is None or ws.headers.get('origin') != expected_origin:
                await ws.close(code=1008)
                return
            await ws.accept()
            receiver = None
            try:
                raw = await asyncio.wait_for(ws.receive_text(), 5)
                if len(raw) > 4096:
                    raise ValueError('Authentication too large')
                auth = json.loads(raw)
                token = auth.get('token') if isinstance(auth, dict) else None
                if not isinstance(token, str) or not secrets.compare_digest(token, row['token']):
                    await ws.close(code=1008)
                    return
                async def listen():
                    while True:
                        await ws.receive_text()
                receiver = asyncio.create_task(listen())
                last = -1
                while self.views.get(pid) is row and not receiver.done():
                    value = row['snapshot']
                    if value['revision'] != last:
                        await asyncio.wait_for(ws.send_json(value), 3)
                        last = value['revision']
                    await asyncio.sleep(.05)
                if self.views.get(pid) is not row:
                    await ws.close(code=1001)
            except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, RuntimeError):
                pass
            finally:
                if receiver:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
