from pathlib import Path
import pytest
from ai_runtime.plugins import Catalog, PluginContext, PluginManager

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    manager = PluginManager(Catalog(tmp_path/'data'), PluginContext(1, 'usb-test', tmp_path/'session', tmp_path))
    manager.add(ROOT/'plugins'/'usb-browser')
    handle = manager.load('usb-browser')
    obj = handle._instance
    calls, mappings = [], []
    def run(args, seconds=15, binary=False):
        calls.append(args)
        out = ''
        if args == ['devices', '-l']:
            out = 'List of devices attached\nphone device usb:1-2\n'
        elif args[-2:] == ['cat', '/proc/net/unix']:
            out = '000: @chrome_devtools_remote\n000: @webview_devtools_remote_123\n'
        elif 'tcp:0' in args:
            mappings.append(['phone', 'tcp:45678', args[-1]])
            out = '45678\n'
        elif args == ['forward', '--list']:
            out = '\n'.join(' '.join(row) for row in mappings)
        elif '--remove' in args:
            mappings[:] = [r for r in mappings if r[1] != args[-1]]
        else:
            raise AssertionError(args)
        return dict(returncode=0, stdout=out, stderr='', truncated=False)
    monkeypatch.setattr(obj.adb, '_run', run)
    yield manager, handle, obj, calls, mappings
    manager.close()


def pages(*ids):
    return {'targets':[dict(id=i, type='page', title=i, url='https://example.test',
        webSocketDebuggerUrl='ws://untrusted-host:9999/devtools/page/'+i) for i in ids]}


def test_selection_forward_rewrite_delegate_and_cleanup(rig, monkeypatch):
    manager, handle, obj, calls, mappings = rig
    cdp=[]
    def call(action, args):
        cdp.append((action, args))
        return pages('one','two') if action == 'targets' else {'ready':True}
    monkeypatch.setattr(obj.dev, 'call', call)
    assert handle.call('browsers')['sockets'] == ['chrome_devtools_remote','webview_devtools_remote_123']
    with pytest.raises(ValueError, match='Select a socket'):
        handle.call('open')
    assert not mappings
    result=handle.call('open', {'socket':'chrome_devtools_remote'})
    assert result['targets'][0]['webSocketDebuggerUrl']=='ws://127.0.0.1:45678/devtools/page/one'
    with pytest.raises(ValueError, match='Choose a target_id'):
        handle.call('connect')
    handle.call('connect', {'target_id':'two'})
    assert ('connect', {'url':'ws://127.0.0.1:45678/devtools/page/two','timeout':5}) in cdp
    handle.call('cdp', {'action':'evaluate','arguments':{'expression':'document.title'}})
    assert cdp[-1] == ('evaluate', {'expression':'document.title'})
    with pytest.raises(ValueError):
        handle.call('cdp', {'action':'connect','arguments':{'url':'ws://elsewhere'}})
    manager.unload('usb-browser')
    assert not mappings
    assert manager.cleanup_report('usb-browser')['ok']


def test_discovery_failure_removes_forward(rig, monkeypatch):
    _, handle, obj, _, mappings = rig
    def fail(*args): raise TimeoutError('browser unavailable')
    monkeypatch.setattr(obj.dev, 'call', fail)
    with pytest.raises(TimeoutError):
        handle.call('open', {'socket':'chrome_devtools_remote'})
    assert not mappings and obj.forward is None


def test_does_not_remove_replaced_forward(rig, monkeypatch):
    _, handle, obj, calls, mappings = rig
    monkeypatch.setattr(obj.dev, 'call', lambda *args:pages('one'))
    handle.call('open', {'socket':'chrome_devtools_remote'})
    mappings[0][2] = 'localabstract:somebody_else'
    handle.call('disconnect')
    assert len(mappings)==1
    assert not any('--remove' in c for c in calls)


def test_cleanup_failure_is_retryable(rig, monkeypatch):
    manager, handle, obj, _, mappings = rig
    monkeypatch.setattr(obj.dev, 'call', lambda *args:pages('one'))
    handle.call('open', {'socket':'chrome_devtools_remote'})
    original=obj.adb._run
    def fail(args, *a, **kw):
        if '--remove' in args: raise TimeoutError('adb stuck')
        return original(args,*a,**kw)
    monkeypatch.setattr(obj.adb, '_run', fail)
    with pytest.raises(RuntimeError, match='cleanup incomplete'):
        manager.unload('usb-browser')
    assert not manager.cleanup_report('usb-browser')['ok']
    monkeypatch.setattr(obj.adb, '_run', original)
    manager.unload('usb-browser')
    assert not mappings
