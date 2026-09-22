import asyncio
import base64
import contextlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from jupyter_client import AsyncKernelManager
from .store import Store

TERMINAL = {'succeeded', 'failed', 'interrupted', 'unknown', 'cancelled'}
OUTPUT_LIMIT = 4 * 1024 * 1024


@dataclass
class Pending:
    execution_id: str
    done: asyncio.Event = field(default_factory=asyncio.Event)
    reply: dict | None = None
    idle: bool = False
    reason: str | None = None
    lost: bool = False
    output_bytes: int = 0
    truncated: bool = False

    def complete(self):
        if self.reply is not None and self.idle:
            self.done.set()


@dataclass
class Session:
    id: str
    manager: object
    client: object
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending: dict = field(default_factory=dict)
    tasks: list = field(default_factory=list)
    active: Pending | None = None
    closing: bool = False


class Runtime:
    def __init__(self, root: Path):
        self.store = Store(root)
        self.sessions = {}
        self.lifecycle_lock = asyncio.Lock()

    def session(self, sid):
        row = self.store.one('SELECT * FROM sessions WHERE id=?', (sid,))
        if not row:
            raise KeyError('session not found')
        return row

    def execution(self, eid):
        row = self.store.one('SELECT * FROM executions WHERE id=?', (eid,))
        if not row:
            raise KeyError('execution not found')
        return row

    async def create(self, cwd):
        if len(self.sessions) >= 8:
            raise ValueError('maximum of 8 live sessions reached')
        cwd = Path(cwd).resolve()
        if not cwd.is_dir():
            raise ValueError('cwd must be an existing directory')
        sid = uuid.uuid4().hex
        self.store.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (sid, str(cwd), 'starting', 1, time.time()))
        await self._start(sid)
        return self.session(sid)

    async def _start(self, sid):
        row = self.session(sid)
        folder = self.store.root / 'sessions' / sid
        folder.mkdir(parents=True, exist_ok=True)
        km = AsyncKernelManager(kernel_name='python3', autorestart=False,
                                connection_file=str(folder / 'connection.json'))
        # Always use this runtime's Python environment, not a global kernelspec interpreter.
        km.kernel_spec.argv = [sys.executable, '-m', 'ipykernel_launcher', '-f', '{connection_file}']
        client = None
        try:
            env = dict(os.environ, AI_RUNTIME_SESSION_DIR=str(folder))
            await km.start_kernel(cwd=row['cwd'], env=env)
            client = km.client()
            client.start_channels()
            await client.wait_for_ready(timeout=30)
            reply = await client.execute_interactive('from ai_runtime import sdk as runtime',
                allow_stdin=False, timeout=15, output_hook=lambda msg: None)
            if reply['content']['status'] != 'ok':
                raise RuntimeError('kernel SDK initialization failed')
            live = Session(sid, km, client)
            self.sessions[sid] = live
            live.tasks = [asyncio.create_task(self._pump(live, channel)) for channel in ('shell', 'iopub')]
            live.tasks.extend([asyncio.create_task(self._worker(live)), asyncio.create_task(self._watch(live))])
            self.store.execute("UPDATE sessions SET status='ready' WHERE id=?", (sid,))
            self.store.event(sid, None, 'session.ready', {'generation': row['generation']})
        except BaseException:
            if client:
                client.stop_channels()
            with contextlib.suppress(Exception):
                await km.shutdown_kernel(now=True)
            self.store.execute("UPDATE sessions SET status='lost' WHERE id=?", (sid,))
            raise

    def submit(self, sid, request_id, code, timeout=30):
        self.session(sid)
        old = self.store.one('SELECT * FROM executions WHERE session_id=? AND request_id=?', (sid, request_id))
        if old:
            if old['code'] != code or old['timeout'] != timeout:
                raise ValueError('request_id already used with different code or timeout')
            return old
        live = self.sessions.get(sid)
        if not live or live.closing or self.session(sid)['status'] != 'ready':
            raise ValueError('session is unavailable; explicitly restart it to create fresh memory')
        if live.queue.qsize() >= 100:
            raise ValueError('session execution queue is full')
        eid = uuid.uuid4().hex
        self.store.execute('INSERT INTO executions VALUES(?,?,?,?,?,?,?,?)',
                           (eid, sid, request_id, code, timeout, 'queued', None, time.time()))
        self.store.event(sid, eid, 'execution.queued', {})
        live.queue.put_nowait(eid)
        return self.execution(eid)

    async def _pump(self, live, channel):
        receive = getattr(live.client, f'get_{channel}_msg')
        try:
            while True:
                msg = await receive()
                parent = msg.get('parent_header', {}).get('msg_id')
                pending = live.pending.get(parent)
                kind, content = msg['msg_type'], msg['content']
                if pending and kind == 'execute_reply':
                    pending.reply = content
                    pending.complete()
                elif pending and kind == 'status' and content.get('execution_state') == 'idle':
                    pending.idle = True
                    pending.complete()
                elif channel == 'iopub' and kind in ('stream', 'display_data', 'update_display_data', 'execute_result', 'error', 'clear_output'):
                    self._output(live.id, pending, kind, content)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._lost(live, f'{channel} reader failed: {exc}')

    def _output(self, sid, pending, kind, content):
        eid = pending.execution_id if pending else None
        encoded_size = len(json.dumps(content).encode('utf-8'))
        if pending:
            pending.output_bytes += encoded_size
            if pending.output_bytes > OUTPUT_LIMIT:
                if not pending.truncated:
                    pending.truncated = True
                    self.store.event(sid, eid, 'output.truncated', {'limit_bytes': OUTPUT_LIMIT})
                return
        data = content.get('data', {})
        # Preserve MIME bundles but store binary images separately.
        for mime, suffix in [('image/png', '.png'), ('image/jpeg', '.jpg')]:
            if mime in data:
                try:
                    raw = base64.b64decode(data[mime], validate=True)
                    target = self.store.root / 'sessions' / sid / 'artifacts' / (uuid.uuid4().hex + suffix)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    artifact = self._artifact(sid, target, mime)
                    data[mime] = {'artifact_id': artifact['id']}
                    self.store.event(sid, eid, 'artifact.created', artifact)
                except (ValueError, OSError) as exc:
                    data[mime] = {'error': str(exc)}
        descriptor = data.get('application/vnd.ai-runtime.artifact+json')
        if descriptor:
            try:
                artifact = self._artifact(sid, Path(descriptor['path']), descriptor['mime'])
                self.store.event(sid, eid, 'artifact.created', artifact)
            except (KeyError, ValueError, OSError) as exc:
                self.store.event(sid, eid, 'artifact.error', {'message': str(exc)})
        self.store.event(sid, eid, 'output.' + kind, content)

    def _artifact(self, sid, path, mime):
        path = path.resolve()
        allowed = (self.store.root / 'sessions' / sid / 'artifacts').resolve()
        if not path.is_relative_to(allowed) or not path.is_file():
            raise ValueError('artifact must be a file inside this session artifact directory')
        aid = uuid.uuid4().hex
        self.store.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)', (aid, sid, str(path), mime, path.stat().st_size))
        return {'id': aid, 'session_id': sid, 'mime': mime, 'size': path.stat().st_size}

    async def _worker(self, live):
        while True:
            eid = await live.queue.get()
            try:
                if self.execution(eid)['status'] != 'queued':
                    continue
                if self.session(live.id)['status'] != 'ready':
                    self._finish(eid, 'cancelled', 'kernel unavailable before dispatch')
                    continue
                row = self.execution(eid)
                pending = Pending(eid)
                live.active = pending
                self.store.execute("UPDATE executions SET status='running' WHERE id=?", (eid,))
                self.store.event(live.id, eid, 'execution.started', {})
                msgid = live.client.execute(row['code'], allow_stdin=False, stop_on_error=False)
                live.pending[msgid] = pending
                try:
                    await asyncio.wait_for(pending.done.wait(), row['timeout'])
                except asyncio.TimeoutError:
                    if pending.reason is None:
                        pending.reason = 'timeout'
                        await self._send_interrupt(live, pending)
                    try:
                        await asyncio.wait_for(pending.done.wait(), 5)
                    except asyncio.TimeoutError:
                        self._lost(live, 'interrupt not confirmed; kernel must be restarted')
                if pending.lost:
                    self._finish(eid, 'unknown', pending.reason)
                elif pending.reply and pending.reply.get('status') == 'ok':
                    # An interrupt request can race with normal completion.
                    self._finish(eid, 'succeeded', pending.reason)
                elif pending.reason:
                    self._finish(eid, 'interrupted', pending.reason)
                else:
                    self._finish(eid, 'failed', (pending.reply or {}).get('ename', 'execution error'))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._finish(eid, 'unknown', str(exc))
                self._lost(live, str(exc))
            finally:
                live.active = None
                live.queue.task_done()

    def _finish(self, eid, status, reason=None):
        row = self.execution(eid)
        self.store.execute('UPDATE executions SET status=?,reason=? WHERE id=?', (status, reason, eid))
        self.store.event(row['session_id'], eid, 'execution.' + status, {'reason': reason})

    async def _send_interrupt(self, live, pending):
        self.store.execute("UPDATE executions SET status='interrupting',reason=? WHERE id=?", (pending.reason, pending.execution_id))
        self.store.event(live.id, pending.execution_id, 'execution.interrupting', {'reason': pending.reason})
        try:
            await live.manager.interrupt_kernel()
        except Exception as exc:
            self._lost(live, f'interrupt failed: {exc}')

    async def interrupt(self, eid):
        row = self.execution(eid)
        if row['status'] == 'queued':
            self._finish(eid, 'cancelled', 'cancelled before dispatch')
        elif row['status'] == 'running':
            live = self.sessions.get(row['session_id'])
            if live and live.active and live.active.execution_id == eid:
                live.active.reason = 'user interrupt'
                await self._send_interrupt(live, live.active)
                # Interrupt requests must not wait until the original execution timeout.
                asyncio.create_task(self._interrupt_deadline(live, live.active))
        return self.execution(eid)

    async def _interrupt_deadline(self, live, pending):
        try:
            await asyncio.wait_for(pending.done.wait(), 5)
        except asyncio.TimeoutError:
            if not live.closing:
                self._lost(live, 'interrupt not confirmed; kernel must be restarted')

    def _lost(self, live, reason):
        if live.closing or self.session(live.id)['status'] == 'lost':
            return
        self.store.execute("UPDATE sessions SET status='lost' WHERE id=?", (live.id,))
        self.store.event(live.id, None, 'session.lost', {'reason': reason})
        if live.active:
            live.active.lost = True
            live.active.reason = reason
            live.active.done.set()

    async def _watch(self, live):
        while True:
            await asyncio.sleep(1)
            if not await live.manager.is_alive():
                self._lost(live, 'kernel process exited')
                return

    async def close_session(self, sid):
        self.session(sid)
        live = self.sessions.pop(sid, None)
        if live:
            live.closing = True
            for task in live.tasks:
                task.cancel()
            await asyncio.gather(*live.tasks, return_exceptions=True)
            try:
                await live.manager.shutdown_kernel(now=True)
            finally:
                live.client.stop_channels()
        for row in self.store.all("SELECT id,status FROM executions WHERE session_id=? AND status IN ('queued','running','interrupting')", (sid,)):
            self._finish(row['id'], 'cancelled' if row['status'] == 'queued' else 'unknown', 'session closed')
        self.store.execute("UPDATE sessions SET status='closed' WHERE id=?", (sid,))
        self.store.event(sid, None, 'session.closed', {})

    async def restart(self, sid):
        async with self.lifecycle_lock:
            await self.close_session(sid)
            self.store.execute("UPDATE sessions SET status='starting',generation=generation+1 WHERE id=?", (sid,))
            await self._start(sid)
            return self.session(sid)

    async def close(self):
        for sid in list(self.sessions):
            await self.close_session(sid)
        self.store.close()
