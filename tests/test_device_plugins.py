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
        status=cdp.call('status')
        assert status['timeouts']==1 and status['late_responses']==1
        assert any(x['method']=='Test.slow' and x.get('late') for x in status['recent_commands'])
        assert status['responsiveness']=='responsive'
        assert status['heartbeat'] is False
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
        connection=cdp.call('connect',{'url':f'ws://127.0.0.1:{port}/new'})
        assert cdp.call('events',{'after':connection['cursor']})['events']==[]
        assert cdp.call('status')['responses']==0
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

def test_prepare_explicit_startup_order_pause_and_disconnect(tmp_path):
    methods=[]
    def target(ws):
        for raw in ws:
            req=json.loads(raw);method=req['method'];methods.append(method)
            if method=='Debugger.enable':
                ws.send(json.dumps({'method':'Debugger.paused','params':{'reason':'other','callFrames':[]}}))
            ws.send(json.dumps({'id':req['id'],'result':{}}))
            if method=='Test.close':
                ws.close(1001,'test shutdown')
    server=serve(target,'127.0.0.1',0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    m,cdp=loaded(tmp_path,'chrome-devtools')
    try:
        cdp.call('connect',{'url':f'ws://127.0.0.1:{server.socket.getsockname()[1]}'})
        assert cdp.call('status')['responsiveness']=='unknown'
        report=cdp.call('prepare',{'release_wait':True,'timeout':2})
        assert methods==['Runtime.runIfWaitingForDebugger','Runtime.enable','Debugger.enable']
        assert not report['ready'] and report['steps'][-1]['status']=='paused'
        assert report['diagnostics']['execution']['default']=='paused'
        cdp.call('command',{'method':'Test.close'})
        cdp._instance.reader.join(2)
        status=cdp.call('status')
        assert not status['connected']
        assert status['last_disconnect']['code']==1001
        assert status['last_disconnect']['reason']=='test shutdown'
    finally:
        m.close();server.shutdown();thread.join(3)


def test_prepare_stops_at_first_timeout_without_release_or_resume(tmp_path):
    methods=[]
    release=threading.Event()
    def target(ws):
        req=json.loads(ws.recv());methods.append(req['method'])
        release.wait(2)
    server=serve(target,'127.0.0.1',0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    m,cdp=loaded(tmp_path,'chrome-devtools')
    try:
        cdp.call('connect',{'url':f'ws://127.0.0.1:{server.socket.getsockname()[1]}'})
        report=cdp.call('prepare',{'timeout':.15})
        assert not report['ready']
        assert methods==['Runtime.enable']
        assert report['diagnostics']['responsiveness']=='unresponsive'
        assert report['steps'][0]['status']=='TimeoutError'
    finally:
        release.set();m.close();server.shutdown();thread.join(3)


def test_adb_diagnose_filters_secrets_and_retains_partial_report(tmp_path,monkeypatch):
    m,adb=loaded(tmp_path,'adb')
    def run(args, seconds=15, binary=False):
        if args==['devices','-l']: output='List of devices attached\none device\n'
        elif args[-1]=='power': raise TimeoutError('timeout')
        elif args[-1]=='window': output='mCurrentFocus=Window{test.app/Main}'
        elif 'pidof' in args: output='123\n'
        else:
            assert '--pid=123' in args
            output=('E runtime: http://host/path?key=hidden download failure:Not Found\n'
                    'E runtime: Authorization: Bearer hidden\n'
                    'E okhttp.OkHttpClient: error secret-content\n'
                    'D runtime: LoadModule success main.js')
        return {'returncode':0,'stdout':output,'stderr':'','truncated':False}
    monkeypatch.setattr(adb._instance,'_run',run)
    try:
        report=adb.call('diagnose',{'package':'test.app'})
        assert report['errors']==[{'step':'power','error':'TimeoutError'}]
        assert report['pids']==['123'] and report['findings']
        assert 'hidden' not in json.dumps(report) and 'secret-content' not in json.dumps(report)
        assert 'Not Found' in json.dumps(report)
        with pytest.raises(ValueError):
            adb.call('diagnose',{'package':'test.app; rm'})
    finally: m.close()


def test_cdp_script_index_source_stack_and_breakpoints(tmp_path):
    commands = []
    def target(ws):
        try:
            for raw in ws:
                req = json.loads(raw)
                commands.append(req)
                method, result = req['method'], {}
                if method == 'Debugger.enable':
                    for i in range(230):
                        ws.send(json.dumps({'method':'Debugger.scriptParsed','params':{
                            'scriptId':str(i),'url':f'game/{i}.js','startLine':0,'endLine':10}}))
                    ws.send(json.dumps({'method':'Debugger.paused','params':{'reason':'other','callFrames':[{
                        'callFrameId':'frame1','functionName':'update','url':'game/0.js',
                        'location':{'scriptId':'0','lineNumber':4,'columnNumber':2}}]}}))
                elif method == 'Debugger.getScriptSource':
                    result = {'scriptSource':'one\ntwo\nthree\nfour'}
                elif method == 'Debugger.setBreakpointByUrl':
                    result = {'breakpointId':'bp1','locations':[]}
                elif method == 'Debugger.resume':
                    ws.send(json.dumps({'method':'Debugger.resumed','params':{}}))
                ws.send(json.dumps({'id':req['id'],'result':result}))
        except Exception:
            pass
    server = serve(target,'127.0.0.1',0)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    m, cdp = loaded(tmp_path,'chrome-devtools')
    url = f'ws://127.0.0.1:{server.socket.getsockname()[1]}/test'
    try:
        cdp.call('connect', {'url':url})
        cdp.call('command', {'method':'Debugger.enable'})
        assert cdp.call('events')['dropped']
        scripts = cdp.call('scripts', {'query':'game/0.js'})
        assert scripts['scripts'][0]['script_id'] == '0'
        assert cdp.call('scripts', {'limit':200})['has_more']
        assert cdp.call('scripts', {'session_id':'other'})['scripts'] == []
        result = cdp.call('source', {'script_id':'0','start_line':2,'line_count':2})
        assert result['source'] == 'two\nthree' and result['has_more']
        stack = cdp.call('stack')
        assert stack['execution'] == 'paused' and stack['pause']['frames'][0]['line'] == 5
        assert cdp.call('breakpoint_set', {'url':'game/0.js','line':5})['breakpointId'] == 'bp1'
        assert commands[-1]['params']['lineNumber'] == 4
        cdp.call('breakpoint_remove', {'breakpoint_id':'bp1'})
        cdp.call('resume')
        assert cdp.call('stack')['pause'] is None
        with pytest.raises(ValueError):
            cdp.call('source', {'script_id':'0','start_line':0})
        cdp.call('connect', {'url':url})
        assert cdp.call('scripts')['scripts'] == []
        assert cdp.call('stack')['execution'] == 'unknown'
    finally:
        m.close();server.shutdown();thread.join(3)
