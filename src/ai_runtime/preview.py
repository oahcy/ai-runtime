"""Live Preview: a kernel-owned, read-only browser view of Python scene state."""
import asyncio
import copy
import json
import math
import secrets
import socket
import threading
import time
from pathlib import Path

DEFAULTS = dict(hue=190.0, speed=0.65, particles=650, glow=0.8,
                rings=5, spread=1.0, title='NEON SINGULARITY')
RANGES = dict(hue=(0, 360), speed=(0, 3), particles=(0, 2000),
              glow=(0, 1.5), rings=(1, 10), spread=(0.5, 1.5))


class Preview:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = dict(DEFAULTS)
        self._revision = 0
        self._history = []
        self._server = self._thread = self._socket = None
        self._token = secrets.token_urlsafe(32)
        self._port = None

    def snapshot(self):
        with self._lock:
            return dict(revision=self._revision, state=dict(self._state),
                        history=copy.deepcopy(self._history))

    def update(self, *, message='Scene updated', **changes):
        if not isinstance(message, str) or len(message) > 300:
            raise ValueError('message must be a string of at most 300 characters')
        for key, value in changes.items():
            if key == 'title':
                if not isinstance(value, str) or len(value) > 80:
                    raise ValueError('title must be a string of at most 80 characters')
            elif key in RANGES:
                low, high = RANGES[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError(f'{key} must be between {low} and {high}')
                if key in ('particles', 'rings') and not isinstance(value, int):
                    raise ValueError(f'{key} must be an integer')
            else:
                raise ValueError(f'Unknown scene parameter: {key}')
        with self._lock:
            self._state.update(changes)
            self._revision += 1
            self._history.append(dict(revision=self._revision, message=message,
                                      changes=dict(changes), time=time.time()))
            self._history = self._history[-30:]
        return self.snapshot()

    def _app(self):
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect
        from fastapi.responses import HTMLResponse
        # FastAPI resolves annotations against module globals.
        globals()['WebSocket'] = WebSocket
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get('/')
        async def page():
            html = Path(__file__).with_name('preview.html').read_text(encoding='utf-8')
            return HTMLResponse(html, headers={
                'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                'X-Content-Type-Options': 'nosniff',
                'Content-Security-Policy': "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
            })

        @app.websocket('/ws')
        async def stream(ws: WebSocket):
            origin = ws.headers.get('origin')
            if origin != f'http://127.0.0.1:{self._port}':
                await ws.close(code=1008)
                return
            await ws.accept()
            receiver = None
            try:
                auth = await asyncio.wait_for(ws.receive_json(), 5)
                token = auth.get('token') if isinstance(auth, dict) else None
                if not isinstance(token, str) or not secrets.compare_digest(token, self._token):
                    await ws.close(code=1008)
                    return
                async def listen():
                    while True:
                        # Client messages only maintain liveness, never execute code.
                        await ws.receive_text()
                receiver = asyncio.create_task(listen())
                last = -1
                while not receiver.done():
                    snap = self.snapshot()
                    if snap['revision'] != last:
                        await ws.send_json(snap)
                        last = snap['revision']
                    await asyncio.sleep(0.05)
            except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, RuntimeError):
                pass
            finally:
                if receiver:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
        return app

    def start(self, port=0):
        if self._thread and self._thread.is_alive():
            return self.info()
        import uvicorn
        sock = socket.socket()
        sock.bind(('127.0.0.1', port))
        sock.listen(128)
        self._port = sock.getsockname()[1]
        self._socket = sock
        self._server = uvicorn.Server(uvicorn.Config(
            self._app(), host='127.0.0.1', port=self._port,
            log_level='error', access_log=False, ws_max_size=4096,
            timeout_graceful_shutdown=1))
        self._thread = threading.Thread(target=self._server.run,
                                        kwargs={'sockets': [sock]}, daemon=True,
                                        name='ai-runtime-preview')
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError('Preview server did not start')
            time.sleep(0.02)
        return self.info()

    def info(self):
        running = bool(self._thread and self._thread.is_alive() and self._server.started)
        return dict(running=running,
                    url=f'http://127.0.0.1:{self._port}/#{self._token}' if running else None,
                    revision=self.snapshot()['revision'])

    def stop(self):
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError('Preview server shutdown not confirmed')
        if self._socket:
            self._socket.close()
        self._thread = self._server = self._socket = None


_default = Preview()
start = _default.start
update = _default.update
snapshot = _default.snapshot
info = _default.info
stop = _default.stop
