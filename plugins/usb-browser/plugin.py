"""Android browser CDP via owned ADB forwards; uses bundled transports."""
import importlib.util
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit, urlunsplit


def transport(name, context):
    # Private instances share this plugin's resource owner, not other loaded handles.
    folder = Path(__file__).resolve().parent.parent / name
    module_name = __name__ + '._' + name.replace('-', '_')
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, folder / 'plugin.py',
                                                     submodule_search_locations=[str(folder)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
    return sys.modules[module_name].create(context)


class UsbBrowser:
    def __init__(self, context):
        self.context = context
        self.adb = transport('adb', context)
        self.dev = transport('chrome-devtools', context)
        self.forward = None
        self.resource = None
        self.pages = {}

    def run(self, args, seconds=5):
        result = self.adb._run(args, seconds)
        if result['returncode'] or result['truncated']:
            raise RuntimeError('ADB failed or output truncated: ' + result['stderr'][:500])
        return result['stdout']

    def browsers(self, serial):
        raw = self.run(['-s', serial, 'shell', 'cat', '/proc/net/unix'])
        sockets = sorted(set(re.findall(r'@([A-Za-z0-9_.-]*devtools_remote[A-Za-z0-9_.-]*)', raw)))
        return {'serial': serial, 'sockets': sockets,
                'hint': 'Open Chrome or a debugging-enabled WebView on the phone' if not sockets else None}

    def remove_forward(self):
        if not self.forward:
            return
        f = self.forward
        # Avoid removing a forward which somebody replaced while we were connected.
        mappings = [line.split() for line in self.run(['forward', '--list']).splitlines()]
        if [f['serial'], 'tcp:' + str(f['port']), 'localabstract:' + f['socket']] in mappings:
            self.run(['-s', f['serial'], 'forward', '--remove', 'tcp:' + str(f['port'])])
        self.context.logger.info('Browser forward released', device=f['serial'], port=f['port'])
        self.forward = None
        self.pages = {}

    def targets(self):
        if not self.forward:
            raise ValueError('Call open first')
        port = self.forward['port']
        result = self.dev.call('targets', {'endpoint': f'http://127.0.0.1:{port}', 'timeout': 5})
        pages = {}
        for item in result['targets']:
            if not isinstance(item, dict) or item.get('type') != 'page':
                continue
            target_id, address = item.get('id'), item.get('webSocketDebuggerUrl')
            if not isinstance(target_id, str) or not isinstance(address, str):
                continue
            parsed = urlsplit(address)
            if parsed.scheme != 'ws' or not parsed.path.startswith('/devtools/page/'):
                continue
            # Always connect through our own forward, never a target-supplied host.
            pages[target_id] = dict(id=target_id, title=item.get('title'), url=item.get('url'),
                webSocketDebuggerUrl=urlunsplit(('ws', f'127.0.0.1:{port}', parsed.path, parsed.query, '')))
        self.pages = pages
        return {**self.forward, 'targets': list(pages.values())}

    def call(self, action, arguments):
        a = dict(arguments)
        allowed = {'configure': {'adb_path', 'serial'}, 'devices': set(),
                   'browsers': {'serial'}, 'open': {'serial', 'socket'}, 'targets': set(),
                   'connect': {'target_id'}, 'cdp': {'action', 'arguments'},
                   'status': set(), 'disconnect': set()}
        if action not in allowed or set(a) - allowed[action]:
            raise ValueError('Unknown action or parameters')
        if action in ('configure', 'devices'):
            return self.adb.call(action, a)
        if action == 'browsers':
            return self.browsers(self.adb._select(a.get('serial')))
        if action == 'open':
            serial = self.adb._select(a.get('serial'))
            sockets = self.browsers(serial)['sockets']
            socket = a.get('socket')
            if socket is None and len(sockets) == 1:
                socket = sockets[0]
            if socket not in sockets:
                raise ValueError('Select a socket returned by browsers; multiple or no browsers found')
            self.close()
            self.context.resources.ensure_open()
            raw = self.run(['-s', serial, 'forward', 'tcp:0', 'localabstract:' + socket]).strip()
            if not raw.isdecimal() or not 1 <= int(raw) <= 65535:
                raise RuntimeError('ADB did not return an allocated port; inspect adb forward --list')
            self.forward = dict(serial=serial, socket=socket, port=int(raw))
            try:
                self.resource = self.context.resources.register('browser-adb-forward', self.remove_forward,
                                                               kind='connection')
                self.context.logger.info('Browser forward created', **self.forward)
                return self.targets()
            except BaseException:
                if self.resource:
                    self.resource.close()
                else:
                    self.remove_forward()
                raise
        if action == 'targets':
            return self.targets()
        if action == 'connect':
            self.targets()
            target_id = a.get('target_id')
            if target_id is None and len(self.pages) == 1:
                target_id = next(iter(self.pages))
            if not isinstance(target_id, str) or target_id not in self.pages:
                raise ValueError('Choose a target_id from targets')
            page = self.pages[target_id]
            self.dev.call('connect', {'url': page['webSocketDebuggerUrl'], 'timeout': 5})
            return {'target': page, 'prepare': self.dev.call('prepare', {'timeout': 5})}
        if action == 'cdp':
            method = a.get('action')
            if method in ('connect', 'targets', 'disconnect'):
                raise ValueError('Use USB browser connection actions')
            params = a.get('arguments', {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError('Expected CDP action and arguments object')
            return self.dev.call(method, params)
        if action == 'status':
            return {'forward': self.forward, 'debugger': self.dev.call('status', {})}
        self.close()
        return {'disconnected': True}

    def close(self):
        # Close CDP before the transport; failed cleanup remains retryable.
        self.dev.close()
        if self.resource:
            self.resource.close()
            self.resource = None
        elif self.forward:
            self.remove_forward()


def create(context):
    return UsbBrowser(context)
