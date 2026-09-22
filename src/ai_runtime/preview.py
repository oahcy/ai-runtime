"""Live Preview: a kernel-owned, read-only browser view of Python scene state."""
import asyncio
import copy
import json
import secrets
import re
from .preview_client import CLIENT_JS
import socket
import threading
import time

DEFAULT_HTML = """<!doctype html><meta charset="utf-8"><title>Runtime state preview</title>
<style>body{background:#0c1421;color:#cce6e9;font:15px monospace;padding:30px}pre{white-space:pre-wrap}</style>
<h1>Live state</h1><p id="status">Connecting…</p><pre id="state"></pre>
<script>
RuntimePreview.onStatus(status => document.getElementById('status').textContent = status);
RuntimePreview.subscribe((state, meta) => {
document.getElementById('state').textContent=JSON.stringify({state, ...meta}, null, 2);
});
</script>"""


class Preview:
    def __init__(self, *, html=None, state=None):
        self._lock = threading.Lock()
        if html is not None and not isinstance(html, str):
            raise ValueError("html must be a string")
        if state is not None and not isinstance(state, dict):
            raise ValueError("state must be a JSON object")
        self._html = DEFAULT_HTML if html is None else html
        self._state = json.loads(json.dumps({} if state is None else state, allow_nan=False))
        self._revision = 0
        self._history = []
        self._server = self._thread = self._socket = None
        self._token = secrets.token_urlsafe(32)
        self._port = None

    def snapshot(self):
        with self._lock:
            return dict(revision=self._revision, state=copy.deepcopy(self._state),
                        history=copy.deepcopy(self._history))

    def update(self, *, message='Scene updated', **changes):
        if not isinstance(message, str) or len(message) > 300:
            raise ValueError('message must be a string of at most 300 characters')
        # Round-trip both validates JSON and detaches caller-owned nested values.
        changes = json.loads(json.dumps(changes, allow_nan=False))
        with self._lock:
            self._state.update(changes)
            self._revision += 1
            self._history.append(dict(revision=self._revision, message=message,
                                      changes=copy.deepcopy(changes), time=time.time()))
            self._history = self._history[-30:]
        return self.snapshot()

    def publish(self, state):
        """Replace a complete JSON frame, without recording per-frame history."""
        if not isinstance(state, dict):
            raise ValueError('state must be a JSON object')
        detached = json.loads(json.dumps(state, allow_nan=False))
        with self._lock:
            self._state = detached
            self._revision += 1
            return self._revision

    def _app(self):
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect
        from fastapi.responses import HTMLResponse
        # FastAPI resolves annotations against module globals.
        globals()['WebSocket'] = WebSocket
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get('/')
        async def page():
            # Install the public client API before any plugin script executes.
            bootstrap = '<script>' + CLIENT_JS + '</script>'
            first_script = re.search(r'<script\b', self._html, re.IGNORECASE)
            index = first_script.start() if first_script else len(self._html)
            html = self._html[:index] + bootstrap + self._html[index:]
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
publish = _default.publish
update = _default.update
snapshot = _default.snapshot
info = _default.info
stop = _default.stop
