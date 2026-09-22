import json
import threading
from pathlib import Path

import pytest

from ai_runtime import sdk
from ai_runtime.plugin_services import Resources
from ai_runtime.plugins import Catalog, PluginContext, PluginManager, scaffold


def manager(tmp_path, source):
    folder=Path(scaffold(tmp_path/'plugin','demo'))
    (folder/'plugin.py').write_text(source,encoding='utf-8')
    m=PluginManager(Catalog(tmp_path/'data'),PluginContext(1,'sid',tmp_path/'session',tmp_path))
    m.add(folder)
    return m


def test_resources_lifo_failure_retry_and_no_reregistration():
    resources=Resources()
    calls=[]
    failing=[True]
    def flaky():
        calls.append('flaky')
        if failing[0]: raise RuntimeError('private error contents')
    resources.register('first',lambda:calls.append('first'))
    resource=resources.register('flaky',flaky)
    resources.register('last',lambda:calls.append('last'))
    result=resources.close_all()
    assert calls==['last','flaky','first']
    assert not result['ok'] and result['remaining'][0]['name']=='flaky'
    assert 'private error contents' not in json.dumps(result)
    with pytest.raises(RuntimeError):
        resources.register('late',lambda:None)
    failing[0]=False
    assert resources.close_all()['ok']
    resource.close()
    assert calls==['last','flaky','first','flaky']


def test_thread_shutdown_failure_is_reported_and_retryable():
    resources=Resources()
    done=threading.Event()
    worker=threading.Thread(target=lambda:done.wait(5),daemon=True)
    resources.track_thread(worker,stop=lambda:None,timeout=.1)
    worker.start()
    try:
        result=resources.close_all()
        assert not result['ok'] and result['remaining'][0]['kind']=='thread'
    finally:
        done.set();worker.join(2)
    assert resources.close_all()['ok']


def test_context_artifacts_are_bound_and_preview_is_cleaned_without_plugin_help(tmp_path,monkeypatch):
    monkeypatch.setenv('AI_RUNTIME_SESSION_DIR',str(tmp_path/'wrong-session'))
    monkeypatch.setattr(sdk,'display',lambda *a,**kw:None)
    m=manager(tmp_path, """
class Plugin:
    def __init__(self, context):
        self.context=context
        self.view=context.preview.create(state={'score':1})
        self.view.start()
    def call(self, action, arguments):
        return self.context.artifacts.save('hello','result.txt','text/plain')
    def close(self):
        pass
def create(context):
    return Plugin(context)
""")
    p=m.load('demo')
    view=p._instance.view
    artifact=p.call('ping')
    assert Path(artifact['path']).is_relative_to(tmp_path/'session'/'artifacts')
    assert Path(artifact['path']).read_text()=='hello'
    assert m.unload('demo')
    assert not view.info()['running']
    report=m.cleanup_report('demo')
    assert report['ok'] and report['results'][0]['kind']=='preview'
    report['results'].clear()
    assert m.cleanup_report('demo')['results']
    with pytest.raises(RuntimeError):
        p.context.artifacts.save('closed')
    with pytest.raises(RuntimeError):
        view.start()


def test_unload_hook_failure_still_cleans_resources_and_blocks_actions(tmp_path):
    m=manager(tmp_path, """
class Plugin:
    def __init__(self,context):
        self.fail=True
        self.cleaned=False
        context.resources.register('connection',self.cleanup,kind='connection')
    def cleanup(self):
        self.cleaned=True
    def call(self,action,arguments):
        return {}
    def close(self):
        if self.fail: raise RuntimeError('hook failed')
def create(context):
    return Plugin(context)
""")
    p=m.load('demo')
    with pytest.raises(RuntimeError,match='cleanup incomplete'):m.unload('demo')
    assert p._instance.cleaned
    assert m.cleanup_report('demo')['hook']['status']=='failed'
    with pytest.raises(ValueError,match='closed'):p.call('ping')
    with pytest.raises(RuntimeError,match='cleanup incomplete'):m.load('demo')
    p._instance.fail=False
    assert m.unload('demo')
    assert m.cleanup_report('demo')['ok']


def test_factory_failure_keeps_cleanup_handle_until_resources_close(tmp_path):
    m=manager(tmp_path, """
class Owner:
    fail=True
    def close(self):
        if self.fail: raise RuntimeError('resource failed')
owner=Owner()
def create(context):
    context.resources.register('factory-resource',owner.close)
    raise ValueError('factory failed')
""")
    with pytest.raises(ValueError,match='factory failed'):m.load('demo')
    assert not m.cleanup_report('demo')['ok']
    handle=m._loaded['demo']
    import sys
    module=sys.modules[handle._module_name]
    module.owner.fail=False
    assert m.unload('demo')
    assert handle._module_name not in sys.modules


def test_contexts_do_not_share_resource_registries(tmp_path):
    m=manager(tmp_path, """
class Plugin:
    def call(self,action,arguments): return {}
    def close(self): pass
def create(context): return Plugin()
""")
    other=PluginManager(m.catalog,PluginContext(1,'other',tmp_path/'other',tmp_path))
    a,b=m.load('demo'),other.load('demo')
    assert a.context.resources is not b.context.resources
    m.unload('demo')
    registration=b.context.resources.register('still-open',lambda:None)
    assert not registration.closed
    other.unload('demo')
