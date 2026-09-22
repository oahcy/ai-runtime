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
