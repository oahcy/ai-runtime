import base64
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from websockets.sync.server import serve

from ai_runtime.plugins import Catalog, PluginContext, PluginManager
from ai_runtime import sdk

ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=')


def loaded(tmp_path, plugin):
    m = PluginManager(Catalog(tmp_path/'data'), PluginContext(1,'test-session',tmp_path/'session',tmp_path))
    m.add(ROOT/'plugins'/plugin)
    return m, m.load(plugin)


def test_cdp_real_socket_events_late_reply_error_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_RUNTIME_SESSION_DIR', str(tmp_path/'session'))
    monkeypatch.setattr(sdk, 'display', lambda *a, **k: None)
    def target(ws):
        try:
            for raw in ws:
                request=json.loads(raw)
                method=request['method']
                if method=='Test.slow':
                    time.sleep(.25)
                if method=='Test.error':
                    ws.send(json.dumps({'id':request['id'],'error':{'code':-32601,'message':'unsupported'}}))
                    continue
                if method=='Page.captureScreenshot':
                    result={'data':base64.b64encode(PNG).decode()}
                else:
                    ws.send(json.dumps({'method':'Runtime.consoleAPICalled','params':{'type':'log'}}))
                    result={'result':{'value':42},'method':method}
                ws.send(json.dumps({'id':request['id'],'result':result}))
        except Exception:
            pass
    server=serve(target,'127.0.0.1',0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    port=server.socket.getsockname()[1]
    m,cdp=loaded(tmp_path,'chrome-devtools')
    try:
        result=cdp.call('connect',{'url':f'devtools://devtools/bundled/inspector.html?v8only=true&ws=127.0.0.1:{port}/test'})
        assert result['connected']
        assert cdp.call('evaluate',{'expression':'40+2'})['result']['value']==42
        events=cdp.call('events',{})
        assert events['events'][0]['method']=='Runtime.consoleAPICalled'
        assert cdp.call('events',{'after':events['cursor']})['events']==[]
        with pytest.raises(TimeoutError,match='outcome unknown'):
            cdp.call('command',{'method':'Test.slow','timeout':.1})
        # The timed-out command's late response must not satisfy the next command.
        assert cdp.call('command',{'method':'Runtime.enable'})['method']=='Runtime.enable'
        with pytest.raises(RuntimeError,match='unsupported'):
            cdp.call('command',{'method':'Test.error'})
        artifact=cdp.call('screenshot',{})['artifact']
        assert Path(artifact['path']).read_bytes()==PNG
        obj=cdp._instance
        with obj.lock:
            for seq in range(obj.sequence+1,obj.sequence+251):
                obj.event_buffer.append({'seq':seq,'method':'test'})
            obj.sequence=seq
        assert cdp.call('events',{'after':0})['dropped']
        m.unload('chrome-devtools')
        assert obj.reader is None and obj.ws is None
    finally:
        m.close();server.shutdown();thread.join(3)


@pytest.mark.parametrize('url',['ws://example.com/x','http://localhost','ws://user:pass@localhost/x','devtools://devtools/inspector'])
def test_cdp_rejects_invalid_connection_url(tmp_path,url):
    m,cdp=loaded(tmp_path,'chrome-devtools')
    try:
        with pytest.raises(ValueError):
            cdp.call('connect',{'url':url})
    finally:
        m.close()


def test_adb_device_selection_binary_artifact_and_argv(tmp_path,monkeypatch):
    monkeypatch.setenv('AI_RUNTIME_SESSION_DIR',str(tmp_path/'session'))
    monkeypatch.setattr(sdk,'display',lambda *a,**k:None)
    m,adb=loaded(tmp_path,'adb')
    adb._instance.executable='fake-adb'
    commands=[]
    def run(argv,**kw):
        assert kw['shell'] is False
        commands.append(argv)
        if argv[1:]==['devices','-l']:
            data=b'List of devices attached\none device model:test\ntwo device model:test\nblocked unauthorized\n'
        elif 'exec-out' in argv: data=PNG
        else: data=b'ok'
        kw['stdout'].write(data)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess,'run',run)
    try:
        assert len(adb.call('devices',{})['devices'])==3
        with pytest.raises(ValueError,match='Specify serial'):
            adb.call('shell',{'command':'echo test'})
        with pytest.raises(ValueError,match='unauthorized'):
            adb.call('shell',{'command':'echo test','serial':'blocked'})
        adb.call('configure',{'serial':'one'})
        assert adb.call('shell',{'command':'echo hello; echo world'})['returncode']==0
        assert commands[-1]==['fake-adb','-s','one','shell','echo hello; echo world']
        artifact=adb.call('screenshot',{})['artifact']
        assert Path(artifact['path']).read_bytes()==PNG
        adb.call('start_app',{'component':'com.test/.Main'})
        assert commands[-1][-5:]==['am','start','-W','-n','com.test/.Main']
        adb.call('forward',{'local':'tcp:5086','remote':'localabstract:debugger'})
        assert commands[-1][-3:]==['forward','tcp:5086','localabstract:debugger']
        with pytest.raises(ValueError):
            adb.call('logcat',{'lines':5000})
        with pytest.raises(ValueError):
            adb.call('run',{'args':['-s','wrong-device']})
    finally:
        m.close()


def test_adb_nonzero_timeout_and_output_limit(tmp_path,monkeypatch):
    m,adb=loaded(tmp_path,'adb')
    adb._instance.executable='fake-adb'
    def failed(argv,**kw):
        kw['stdout'].write(b'x'*(1024*1024+3))
        kw['stderr'].write(b'failure')
        return SimpleNamespace(returncode=2)
    monkeypatch.setattr(subprocess,'run',failed)
    result=adb._instance._run(['test'])
    assert result['returncode']==2 and result['truncated']
    assert len(result['stdout'])==1024*1024
    def timed_out(argv,**kw):
        raise subprocess.TimeoutExpired(argv,kw['timeout'])
    monkeypatch.setattr(subprocess,'run',timed_out)
    with pytest.raises(TimeoutError,match='remote effects may remain'):
        adb._instance._run(['test'])
    m.close()
