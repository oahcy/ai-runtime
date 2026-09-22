"""Plugin-bound services and explicit, cooperative resource ownership."""
import copy
import math
import threading
import time
import uuid
from collections import deque


class Resource:
    def __init__(self, owner, name, kind, close):
        self.id = uuid.uuid4().hex
        self.name, self.kind = name, kind
        self._owner, self._close = owner, close
        self._lock = threading.RLock()
        self.closed = False
        self.error = None

    def info(self):
        return dict(id=self.id, name=self.name, kind=self.kind,
                    status='closed' if self.closed else 'failed' if self.error else 'active',
                    error=self.error)

    def close(self):
        with self._lock:
            if self.closed:
                return self.info()
            started = time.monotonic()
            try:
                self._close()
            except BaseException as exc:
                self.error = type(exc).__name__
                self._owner._record(self, started)
                raise
            self.closed, self.error = True, None
            self._owner._record(self, started)
            return self.info()


class Resources:
    """Own only explicitly registered resources. Never kill arbitrary threads."""
    def __init__(self):
        self._lock = threading.RLock()
        self._entries = {}
        self._history = deque(maxlen=100)
        self._closing = False

    def ensure_open(self):
        with self._lock:
            if self._closing:
                raise RuntimeError('Plugin services are closing or closed')

    def register(self, name, close, *, kind='resource'):
        if not isinstance(name, str) or not name or len(name) > 100:
            raise ValueError('resource name must be 1..100 characters')
        if not isinstance(kind, str) or not kind or len(kind) > 40 or not callable(close):
            raise ValueError('Expected resource kind and callable cleanup')
        with self._lock:
            self.ensure_open()
            entry = Resource(self, name, kind, close)
            self._entries[entry.id] = entry
            return entry

    def _record(self, entry, started):
        with self._lock:
            result = entry.info()
            result['duration_ms'] = round((time.monotonic() - started) * 1000)
            self._history.append(result)
            if entry.closed:
                self._entries.pop(entry.id, None)

    def track_thread(self, thread, *, stop, timeout=3, name=None):
        if not isinstance(thread, threading.Thread) or not callable(stop):
            raise ValueError('Expected a Thread and a cooperative stop callback')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not .1 <= timeout <= 30:
            raise ValueError('thread timeout must be 0.1..30 seconds')

        def close():
            stop()
            if thread.ident is not None:
                if thread is threading.current_thread():
                    raise RuntimeError('A thread cannot join itself')
                thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError('Thread shutdown not confirmed')
        return self.register(name or thread.name, close, kind='thread')

    def begin_close(self):
        with self._lock:
            self._closing = True

    def close_all(self):
        self.begin_close()
        with self._lock:
            entries = list(self._entries.values())
        for entry in reversed(entries):
            try:
                entry.close()
            except BaseException:
                # Continue cleaning independent resources; retain failures for retry.
                pass
        return self.report()

    def report(self):
        with self._lock:
            return copy.deepcopy(dict(
                ok=not self._entries,
                remaining=[entry.info() for entry in self._entries.values()],
                results=list(self._history),
            ))


class ManagedPreview:
    def __init__(self, view, resource):
        self._view, self._resource = view, resource

    def _ensure_open(self):
        self._resource._owner.ensure_open()
        if self._resource.closed:
            raise RuntimeError('Preview is closed; create a new preview')

    def start(self, port=0):
        self._ensure_open()
        return self._view.start(port)

    def publish(self, state):
        # A registered worker may finish a frame while shutdown is in progress.
        if self._resource.closed:
            raise RuntimeError('Preview is closed')
        return self._view.publish(state)

    def update(self, **changes):
        if self._resource.closed:
            raise RuntimeError('Preview is closed')
        return self._view.update(**changes)

    def snapshot(self):
        return self._view.snapshot()

    def info(self):
        return self._view.info()

    def stop(self):
        return self._resource.close()


class PreviewService:
    def __init__(self, resources):
        self._resources = resources

    def create(self, *, html=None, state=None, name='preview'):
        from .preview import Preview
        self._resources.ensure_open()
        view = Preview(html=html, state=state)
        resource = self._resources.register(name, view.stop, kind='preview')
        return ManagedPreview(view, resource)


class ArtifactService:
    def __init__(self, session_dir, resources):
        self._session_dir, self._resources = session_dir, resources

    def save(self, data, name='artifact', mime='application/octet-stream'):
        from .sdk import _save_artifact
        self._resources.ensure_open()
        return _save_artifact(self._session_dir, data, name, mime)
