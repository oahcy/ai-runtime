"""Explicitly paired game-side JS transport, independent from CDP."""
import asyncio
from collections import deque
import concurrent.futures
import ipaddress
import json
import math
from pathlib import Path
import secrets
import socket
import threading
import time
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
import uvicorn


class GameAgent:
    def __init__(self, context):
        self.context = context
        self.server = self.thread = self.resource = self.loop = None
        self.targets = {}
        self.pending = {}
        self.history = deque(maxlen=200)
        self.seq = 0
        self.lock = threading.RLock()
        self.token = ''
        self.expires = 0
        self.base = None

    def event(self, kind, **fields):
        with self.lock:
            self.seq += 1
            self.history.append(dict(seq=self.seq, kind=kind, **fields))

    def authorized(self, token):
        with self.lock:
            return bool(self.token) and time.monotonic() < self.expires and secrets.compare_digest(token, self.token)

    def pair(self):
        if not self.server or not self.server.started:
            raise RuntimeError('Start the listener first')
        with self.lock:
            self.token = secrets.token_urlsafe(32)
            self.expires = time.monotonic() + 300
            url = f'{self.base}/agent.js?token={self.token}'
        config = dict(url=self.base.replace('http:', 'ws:', 1) + '/ws', token=self.token)
        return dict(url=url, expires_in=300, config=config, agent_file=str(Path(__file__).with_name('agent.js')),
                    loader="const aiAgent = require('./ai-runtime-agent.js')(" + json.dumps(config) + ");")

    def start(self, host='127.0.0.1', port=0):
        if self.server:
            raise RuntimeError('Listener already started; use pair or stop')
        address = ipaddress.IPv4Address(host)
        private = any(address in ipaddress.IPv4Network(net) for net in
                      ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
        if not (address.is_loopback or private):
            raise ValueError('host must be a local loopback or RFC1918 IPv4 address')
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('port must be 0..65535')
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get('/agent.js')
        async def script(token: str = ''):
            if not self.authorized(token):
                return Response('Pairing expired or unauthorized', status_code=403)
            source = Path(__file__).with_name('agent.js').read_text(encoding='utf-8')
            return Response(source,
                            media_type='application/javascript',
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

        @app.websocket('/ws')
        async def connected(ws: WebSocket):
            # DevTools-backed SocketTask may send Origin. Authenticate the first
            # message using the short-lived secret, never Origin or cookies.
            await ws.accept()
            self.event('handshake', origin=(ws.headers.get('origin') or '')[:256])
            target = None
            try:
                hello = json.loads(await asyncio.wait_for(ws.receive_text(), 5))
                if not isinstance(hello, dict) or hello.get('type') != 'hello' or not isinstance(hello.get('token'), str) or not self.authorized(hello['token']):
                    self.event('rejected', reason='invalid_or_expired_pairing')
                    await ws.close(code=1008, reason='Invalid or expired pairing; request new config')
                    return
                with self.lock:
                    full = len(self.targets) >= 4
                    if not full:
                        target = uuid.uuid4().hex
                        self.targets[target] = ws
                if full:
                    self.event('rejected', reason='target_limit')
                    await ws.close(code=1013)
                    return
                self.event('connected', target_id=target)
                await ws.send_json(dict(type='welcome', target_id=target))
                while True:
                    row = json.loads(await ws.receive_text())
                    if not isinstance(row, dict):
                        raise ValueError('Invalid message')
                    if row.get('type') == 'result' and isinstance(row.get('id'), str):
                        future = self.pending.get((target, row['id']))
                        if future and not future.done():
                            future.set_result({k: row[k] for k in ('value', 'error') if k in row})
                    elif row.get('type') == 'log':
                        self.event('log', target_id=target, level=str(row.get('level', 'log'))[:20],
                                   message=str(row.get('message', ''))[:4000])
            except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, TypeError) as exc:
                if not target:
                    self.event('handshake_failed', reason=type(exc).__name__)
            finally:
                if target:
                    with self.lock:
                        self.targets.pop(target, None)
                    for (tid, _), future in list(self.pending.items()):
                        if tid == target and not future.done():
                            future.set_exception(RuntimeError('Target disconnected; outcome unknown'))
                    self.event('disconnected', target_id=target)
                try:
                    await ws.close()
                except RuntimeError:
                    pass

        sock = socket.socket()
        try:
            sock.bind((host, port))
            self.base = f'http://{host}:{sock.getsockname()[1]}'
            self.server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False,
                                         log_level='critical', ws_max_size=131072,
                                         ws_ping_interval=20, ws_ping_timeout=20))
            def run():
                async def serve():
                    self.loop = asyncio.get_running_loop()
                    await self.server.serve(sockets=[sock])
                try:
                    asyncio.run(serve())
                finally:
                    sock.close()
            self.thread = threading.Thread(target=run, name='game-agent', daemon=True)
            self.thread.start()
            self.resource = self.context.resources.register('game-agent-server', self._shutdown, kind='server')
            deadline = time.monotonic() + 5
            while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            if not self.server.started:
                raise RuntimeError('Game agent listener did not start')
            self.context.logger.info('Game agent listener started', address=self.base)
            return self.pair()
        except Exception:
            if self.resource:
                self.resource.close()
            else:
                self._shutdown()
                sock.close()
            raise

    async def _command(self, target, command, timeout):
        ws = self.targets.get(target)
        if ws is None:
            raise ValueError('Unknown or disconnected target_id')
        key = (target, uuid.uuid4().hex)
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await ws.send_json(dict(type='command', id=key[1], command=command))
            try:
                return await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                raise TimeoutError('JS response timed out; outcome unknown, do not replay') from None
        finally:
            self.pending.pop(key, None)

    def _shutdown(self):
        if not self.server:
            return
        self.token = ''
        self.server.should_exit = True
        if self.thread and self.thread.ident is not None:
            self.thread.join(8)
            if self.thread.is_alive():
                raise RuntimeError('Game agent shutdown not confirmed')
        self.server = self.thread = self.loop = None
        self.base = None

    def close(self):
        if self.resource:
            self.resource.close()
            self.resource = None

    def call(self, action, arguments):
        allowed = {'start': {'host', 'port'}, 'pair': set(), 'status': set(),
                   'operate': {'target_id', 'command', 'timeout'}, 'events': {'after'}, 'stop': set()}
        if action not in allowed or set(arguments) - allowed[action]:
            raise ValueError('Unknown action or parameters')
        if action == 'start':
            return self.start(**arguments)
        if action == 'pair':
            return self.pair()
        if action == 'stop':
            self.close()
            return {'stopped': True}
        if action == 'status':
            with self.lock:
                return dict(running=bool(self.server and self.server.started), address=self.base,
                            targets=[{'target_id': tid} for tid in self.targets], protocol='game-agent-v2',
                            capabilities=['roots', 'get', 'set', 'call', 'keys', 'release', 'logs'], cdp=False)
        if action == 'events':
            after = arguments.get('after', 0)
            if type(after) is not int or after < 0:
                raise ValueError('after must be a nonnegative integer')
            with self.lock:
                return dict(events=[r for r in self.history if r['seq'] > after], cursor=self.seq,
                            truncated=bool(self.history and after < self.history[0]['seq'] - 1))
        target, command, timeout = arguments.get('target_id'), arguments.get('command'), arguments.get('timeout', 5)
        if not isinstance(target, str) or not isinstance(command, dict) or command.get('op') not in ('roots', 'get', 'set', 'call', 'keys', 'release'):
            raise ValueError('target_id and a supported command required')
        if len(json.dumps(command, allow_nan=False).encode()) > 65536:
            raise ValueError('Command exceeds 64 KiB')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 30:
            raise ValueError('timeout must be 0.1..30')
        if not self.loop or not self.server:
            raise RuntimeError('Listener not started')
        task = asyncio.run_coroutine_threadsafe(self._command(target, command, timeout), self.loop)
        try:
            return task.result(timeout + 2)
        except concurrent.futures.TimeoutError:
            task.cancel()
            raise TimeoutError('JS response timed out; outcome unknown, do not replay') from None


def create(context):
    return GameAgent(context)
