"""Imported as `runtime` inside each kernel. No privileges beyond the kernel user."""
import json
import os
import uuid
from pathlib import Path
from IPython.display import display


def _root():
    return Path(os.environ['AI_RUNTIME_SESSION_DIR'])


def save_artifact(data: bytes | str, name='artifact', mime='application/octet-stream'):
    folder = _root() / 'artifacts'
    folder.mkdir(parents=True, exist_ok=True)
    # Names are labels, never paths supplied by callers.
    suffix = Path(name).suffix[:16]
    target = folder / (uuid.uuid4().hex + suffix)
    content = data.encode('utf-8') if isinstance(data, str) else data
    target.write_bytes(content)
    result = {'path': str(target), 'name': Path(name).name, 'mime': mime}
    display({'application/vnd.ai-runtime.artifact+json': result}, raw=True)
    return result


def checkpoint_save(state: dict):
    """Explicit JSON state, never a snapshot of live Python objects."""
    target = _root() / 'checkpoint.json'
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    temp.replace(target)
    return state


def checkpoint_load():
    target = _root() / 'checkpoint.json'
    return json.loads(target.read_text(encoding='utf-8')) if target.exists() else None


# Kernel-owned browser preview; starts only when explicitly requested.
from . import preview


class _Plugins:
    """Lazy, session-scoped plugin manager exposed as runtime.plugins."""
    def __init__(self):
        self._manager = None

    def _get_manager(self):
        if self._manager is None:
            import atexit
            from .plugins import Catalog, PluginContext, PluginManager
            folder = _root().resolve()
            self._manager = PluginManager(
                Catalog(folder.parent.parent),
                PluginContext(1, folder.name, folder, Path.cwd()))
            atexit.register(self._manager.close)
        return self._manager

    def list(self):
        return self._get_manager().list()

    def get(self, plugin_id):
        return self._get_manager().get(plugin_id)

    def add(self, path):
        return self._get_manager().add(path)

    def load(self, plugin_id):
        return self._get_manager().load(plugin_id)

    def unload(self, plugin_id):
        return self._get_manager().unload(plugin_id)


plugins = _Plugins()
