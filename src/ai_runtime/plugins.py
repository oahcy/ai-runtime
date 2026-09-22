"""Runtime plugin contract v1: local discovery, registration and kernel lifecycle."""
import argparse
import copy
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from .observability import plugin_event, PluginLogger

ID = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')


def json_copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def read_manifest(folder):
    folder = Path(folder).resolve()
    try:
        manifest = json.loads((folder / 'plugin.json').read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'Cannot read plugin.json: {exc}') from None
    if not isinstance(manifest, dict):
        raise ValueError('plugin.json must contain an object')
    if not isinstance(manifest.get('id'), str) or not ID.fullmatch(manifest['id']):
        raise ValueError('Invalid plugin id')
    if type(manifest.get('api_version')) is not int or manifest['api_version'] != 1:
        raise ValueError('Unsupported plugin api_version; expected 1')
    for key in ('name', 'version', 'description'):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f'Missing plugin {key}')
    entry = manifest.get('entry')
    if not isinstance(entry, str) or Path(entry).is_absolute():
        raise ValueError('entry must be a relative Python file')
    target = (folder / entry).resolve()
    if not target.is_relative_to(folder) or target.suffix != '.py' or not target.is_file():
        raise ValueError('entry must resolve to a Python file within the plugin')
    actions = manifest.get('actions')
    if not isinstance(actions, dict) or not actions:
        raise ValueError('actions must be a non-empty object')
    for name, metadata in actions.items():
        if not ID.fullmatch(name) or not isinstance(metadata, dict) or not isinstance(metadata.get('description'), str):
            raise ValueError('Each action needs a valid name and description')
        if not isinstance(metadata.get('parameters'), dict):
            raise ValueError('Each action needs a parameters description object')
    return manifest


class Catalog:
    """Register trusted local folders; reading metadata never imports plugin code."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'plugins.sqlite3'
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS plugins (id TEXT PRIMARY KEY, path TEXT NOT NULL, manifest TEXT NOT NULL, added_at REAL NOT NULL)')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def list(self):
        with self._db() as db:
            rows = db.execute('SELECT id,path,manifest,added_at FROM plugins ORDER BY id').fetchall()
        return [dict(id=r[0], path=r[1], manifest=json.loads(r[2]), added_at=r[3]) for r in rows]

    def get(self, plugin_id):
        for row in self.list():
            if row['id'] == plugin_id:
                return row
        raise KeyError(f'Plugin not registered: {plugin_id}')

    def add(self, path):
        path = Path(path)
        if not path.is_absolute():
            raise ValueError('Plugin path must be absolute')
        path = path.resolve()
        manifest = read_manifest(path)
        encoded = json.dumps(manifest, sort_keys=True, allow_nan=False)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT path,manifest FROM plugins WHERE id=?', (manifest['id'],)).fetchone()
            if row:
                if row != (str(path), encoded):
                    raise ValueError('Plugin id already registered with another path or manifest')
            else:
                db.execute('INSERT INTO plugins VALUES(?,?,?,?)', (manifest['id'], str(path), encoded, time.time()))
        plugin_event('plugin.registered' if not row else 'plugin.registration_reused', plugin=manifest['id'], version=manifest['version'], path=str(path))
        return self.get(manifest['id'])

    def refresh(self, plugin_id):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT path FROM plugins WHERE id=?', (plugin_id,)).fetchone()
            if row is None:
                raise KeyError(f'Plugin not registered: {plugin_id}')
            manifest = read_manifest(row[0])
            if manifest['id'] != plugin_id:
                raise ValueError('Cannot change plugin id during refresh')
            db.execute('UPDATE plugins SET manifest=? WHERE id=?',
                       (json.dumps(manifest, sort_keys=True, allow_nan=False), plugin_id))
        plugin_event('plugin.refreshed', plugin=plugin_id, version=manifest['version'])
        return self.get(plugin_id)


@dataclass(frozen=True)
class PluginContext:
    api_version: int
    session_id: str
    session_dir: Path
    workdir: Path
    logger: PluginLogger | None = None


class Plugin(Protocol):
    def call(self, action: str, arguments: dict) -> object: ...
    def close(self) -> None: ...


class PluginHandle:
    def __init__(self, plugin_id, manifest, instance, module_name):
        self.id = plugin_id
        self._manifest = manifest
        self._instance = instance
        self._module_name = module_name
        self._closed = False

    def call(self, action, arguments=None):
        if self._closed:
            raise ValueError('Plugin handle is closed')
        if action not in self._manifest['actions']:
            raise ValueError(f'Unknown plugin action: {action}')
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError('arguments must be a JSON object')
        # The plugin validates action-specific parameters. Manifest descriptions are not a sandbox.
        plugin_event('plugin.call', plugin=self.id, action=action, arguments=arguments)
        started = time.monotonic()
        try:
            result = json_copy(self._instance.call(action, json_copy(arguments)))
        except BaseException as exc:
            plugin_event('plugin.call_failed', plugin=self.id, action=action, error=type(exc).__name__)
            raise
        plugin_event('plugin.call_completed', plugin=self.id, action=action, duration_ms=round((time.monotonic()-started)*1000))
        return result

    def describe(self):
        return copy.deepcopy(self._manifest)


class PluginManager:
    """One instance per kernel. Plugin objects are never created inside the daemon."""
    def __init__(self, catalog, context):
        self.catalog, self.context = catalog, context
        self._loaded = {}

    def list(self):
        return [dict(row, loaded=row['id'] in self._loaded) for row in self.catalog.list()]

    def get(self, plugin_id):
        return dict(self.catalog.get(plugin_id), loaded=plugin_id in self._loaded)

    def add(self, path):
        return self.catalog.add(path)

    @staticmethod
    def _forget(name):
        for key in tuple(sys.modules):
            if key == name or key.startswith(name + '.'):
                del sys.modules[key]

    def load(self, plugin_id):
        if plugin_id in self._loaded:
            return self._loaded[plugin_id]
        row = self.catalog.get(plugin_id)
        manifest = read_manifest(row['path'])
        if manifest != row['manifest']:
            raise ValueError('Plugin manifest changed since registration')
        name = '_ai_runtime_plugin_' + uuid.uuid4().hex
        folder = Path(row['path'])
        spec = importlib.util.spec_from_file_location(name, folder / manifest['entry'],
                                                      submodule_search_locations=[str(folder)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        instance = None
        plugin_event('plugin.loading', plugin=plugin_id, version=manifest['version'])
        try:
            spec.loader.exec_module(module)
            instance = module.create(replace(self.context, logger=PluginLogger(plugin_id)))
            if not callable(getattr(instance, 'call', None)) or not callable(getattr(instance, 'close', None)):
                raise ValueError('Plugin must implement call(action, arguments) and close()')
            handle = PluginHandle(plugin_id, manifest, instance, name)
            self._loaded[plugin_id] = handle
            plugin_event('plugin.loaded', plugin=plugin_id, version=manifest['version'])
            return handle
        except BaseException as exc:
            plugin_event('plugin.load_failed', plugin=plugin_id, error=type(exc).__name__)
            try:
                if instance is not None and callable(getattr(instance, 'close', None)):
                    instance.close()
            finally:
                self._forget(name)
            raise

    def unload(self, plugin_id):
        handle = self._loaded.get(plugin_id)
        if not handle:
            return False
        # If cleanup fails, retain the handle so callers can inspect/retry cleanup.
        plugin_event('plugin.unloading', plugin=plugin_id)
        try:
            handle._instance.close()
        except BaseException as exc:
            plugin_event('plugin.unload_failed', plugin=plugin_id, error=type(exc).__name__)
            raise
        handle._closed = True
        del self._loaded[plugin_id]
        self._forget(handle._module_name)
        plugin_event('plugin.unloaded', plugin=plugin_id)
        return True

    def close(self):
        errors = []
        for plugin_id in tuple(self._loaded):
            try:
                self.unload(plugin_id)
            except Exception as exc:
                errors.append(f'{plugin_id}: {exc}')
        if errors:
            raise RuntimeError('; '.join(errors))


def scaffold(path, plugin_id):
    if not ID.fullmatch(plugin_id):
        raise ValueError('Invalid plugin id')
    path = Path(path).resolve()
    if path.exists():
        raise ValueError('Scaffold target must not exist')
    path.mkdir(parents=True)
    manifest = dict(id=plugin_id, name=plugin_id, version='0.1.0', api_version=1,
        description='Local Runtime plugin', entry='plugin.py',
        actions={'ping': {'description': 'Return plugin session context', 'parameters': {}}})
    (path / 'plugin.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    (path / 'plugin.py').write_text(
        'class LocalPlugin:\n'
        '    def __init__(self, context):\n        self.context = context\n'
        '    def call(self, action, arguments):\n'
        '        if action != "ping" or arguments:\n            raise ValueError("Expected ping with no arguments")\n'
        '        return {"session_id": self.context.session_id, "ok": True}\n'
        '    def close(self):\n        pass\n\n'
        'def create(context):\n    return LocalPlugin(context)\n', encoding='utf-8')
    return str(path)


def main():
    parser = argparse.ArgumentParser(description='Local ai-runtime plugins')
    parser.add_argument('--data-dir', type=Path, default=Path('.runtime'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    refresh = sub.add_parser('refresh'); refresh.add_argument('plugin_id')
    get = sub.add_parser('get'); get.add_argument('plugin_id')
    add = sub.add_parser('add'); add.add_argument('path', type=Path)
    new = sub.add_parser('new'); new.add_argument('path', type=Path); new.add_argument('--id', required=True)
    args = parser.parse_args()
    if args.command == 'new':
        result = scaffold(args.path, args.id)
    else:
        catalog = Catalog(args.data_dir)
        if args.command == 'list': result = catalog.list()
        elif args.command == 'refresh': result = catalog.refresh(args.plugin_id)
        elif args.command == 'get': result = catalog.get(args.plugin_id)
        else: result = catalog.add(args.path.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
