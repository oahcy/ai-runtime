"""Kernel-side outbound state publisher; never opens a listening port."""
import inspect
import json
import os
import threading
import uuid
from urllib.parse import urlparse
from websockets.sync.client import connect


class HostedPreview:
    def __init__(self, preview):
        self.preview = preview
        self.base = os.environ['AI_RUNTIME_PREVIEW_URL'].rstrip('/')
        parsed = urlparse(self.base)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.path not in ('', '/') or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('Preview host must be a loopback HTTP origin')
        self.sid = os.environ['AI_RUNTIME_SESSION_ID']
        self.key = os.environ['AI_RUNTIME_PREVIEW_KEY']
        self.id = uuid.uuid4().hex
        self.ws = self.thread = None
        self.stop_event = threading.Event()
        self.stopped = threading.Event()
        self.error = None

    def start(self):
        options = dict(open_timeout=5, close_timeout=1, max_size=4096,
                       additional_headers={'Authorization': 'Bearer ' + self.key})
        parameters = inspect.signature(connect).parameters
        if 'proxy' in parameters: options['proxy'] = None
        if 'ping_interval' in parameters: options['ping_interval'] = None
        ws = connect(self.base.replace('http:', 'ws:') + f'/_internal/previews/{self.sid}/{self.id}', **options)
        try:
            ws.send(json.dumps({'html': self.preview._html, 'token': self.preview._token, 'name': self.preview.name,
                                'snapshot': self.preview.snapshot()}, allow_nan=False))
            if json.loads(ws.recv(timeout=5)).get('ok') is not True:
                raise RuntimeError('Preview registration rejected')
        except BaseException:
            ws.close()
            raise
        self.ws = ws
        self.thread = threading.Thread(target=self._run, daemon=True, name='runtime-preview-publisher')
        try:
            self.thread.start()
        except BaseException:
            ws.close()
            raise

    def _run(self):
        last = -1
        try:
            while not self.stop_event.is_set():
                value = self.preview.snapshot()
                message = {'type': 'state', 'snapshot': value} if value['revision'] != last else {'type': 'ping'}
                self.ws.send(json.dumps(message, allow_nan=False))
                if json.loads(self.ws.recv(timeout=3)).get('ok') is not True:
                    raise RuntimeError('Preview update rejected')
                last = value['revision']
                self.stop_event.wait(.05)
            self.ws.send(json.dumps({'type': 'stop'}))
            if json.loads(self.ws.recv(timeout=3)).get('stopped') is True:
                self.stopped.set()
        except Exception as exc:
            self.error = type(exc).__name__
        finally:
            self.ws.close()

    def info(self):
        running = bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set())
        return dict(running=running, url=f'{self.base}/previews/{self.id}/#{self.preview._token}' if running else None,
                    revision=self.preview.snapshot()['revision'], mode='hosted', error=self.error)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(5)
            if self.thread.is_alive():
                raise RuntimeError('Preview publisher shutdown not confirmed')
        # A closed producer channel also invalidates its server-side preview.
        if self.ws:
            self.ws.close()
