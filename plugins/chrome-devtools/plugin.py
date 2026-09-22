"""Persistent loopback Chrome DevTools Protocol client."""
import base64
from collections import deque
import inspect
import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler
from websockets.sync.client import connect
from ai_runtime.sdk import save_artifact


def timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.1 <= value <= 120:
        raise ValueError('timeout must be 0.1..120 seconds')
    return value


def websocket_url(value):
    if not isinstance(value, str):
        raise ValueError('url must be a string')
    if value.startswith(('devtools://', 'chrome-devtools://')):
        query = parse_qs(urlparse(value).query)
        if 'ws' in query: value = 'ws://' + query['ws'][0]
        elif 'wss' in query: value = 'wss://' + query['wss'][0]
        else: raise ValueError('DevTools URL needs ws or wss query parameter')
    parsed = urlparse(value)
    if parsed.scheme not in ('ws', 'wss') or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Expected a loopback ws:// or wss:// debugger URL')
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Debugger discovery redirects are not allowed')


class DevTools:
    def __init__(self, context):
        self.context = context
        self.ws = None
        self.reader = None
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.pending = {}
        self.event_buffer = deque(maxlen=200)
        self.sequence = 0
        self.request_id = 0
        self.url = None
        self.last_error = None
        self.last_disconnect = None
        self.activity = {}
        self.protocol = {}
        self.history = deque(maxlen=50)
        self.response_count = 0
        self.timed_out = 0
        self.late_responses = 0
        self.last_outcome = None
        self.heartbeat = False

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _status(self):
        with self.lock:
            return {
                'connected': self.ws is not None, 'url': self.url,
                'last_error': self.last_error, 'last_disconnect': self.last_disconnect,
                'transport': 'connected' if self.ws is not None else 'disconnected',
                'responsiveness': ('unresponsive' if self.last_outcome == 'timeout' else
                                   'responsive' if self.last_outcome == 'response' else 'unknown'),
                'execution': dict(self.protocol), 'activity': dict(self.activity),
                'responses': self.response_count, 'timeouts': self.timed_out,
                'late_responses': self.late_responses, 'heartbeat': self.heartbeat,
                'pending': len(self.pending), 'recent_commands': [dict(x) for x in self.history],
            }

    def _prepare(self, arguments):
        seconds = timeout(arguments.get('timeout', 6))
        release = arguments.get('release_wait', False)
        session = arguments.get('session_id')
        if type(release) is not bool:
            raise ValueError('release_wait must be boolean')
        if session is not None and (not isinstance(session, str) or not session):
            raise ValueError('session_id must be a nonempty CDP session string')
        deadline = time.monotonic() + seconds
        steps = []
        commands = []
        if release:
            commands.append(('Runtime.runIfWaitingForDebugger', {}))
        commands += [('Runtime.enable', {}),
                     ('Debugger.enable', {}),
                     ('Runtime.evaluate', {'expression': '1 + 1', 'returnByValue': True, 'awaitPromise': False})]
        for method, params in commands:
            remaining = deadline - time.monotonic()
            if remaining < .1:
                steps.append({'method': method, 'status': 'budget_exhausted'})
                break
            try:
                result = self._command(method, params, remaining, session)
                if method == 'Runtime.evaluate' and result.get('result', {}).get('value') != 2:
                    steps.append({'method': method, 'status': 'probe_failed'})
                    break
                steps.append({'method': method, 'status': 'ok'})
                if method == 'Debugger.enable':
                    with self.lock:
                        paused = self.protocol.get(session or 'default') == 'paused'
                    if paused:
                        steps.append({'method': 'Runtime.evaluate', 'status': 'paused'})
                        break
            except (TimeoutError, RuntimeError, ValueError) as exc:
                steps.append({'method': method, 'status': type(exc).__name__})
                break
        ready = bool(steps and steps[-1] == {'method': 'Runtime.evaluate', 'status': 'ok'})
        return {'ready': ready, 'steps': steps, 'diagnostics': self._status(),
                'next': ('Read application state and exceptions' if ready else
                         'Inspect ADB diagnose and events; no retry or resume was performed automatically')}

    def _receive(self, ws):
        error = 'Debugger disconnected'
        try:
            for raw in ws:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                with self.lock:
                    self.activity['last_receive'] = self._now()
                    if 'id' in message:
                        self.response_count += 1
                        self.last_outcome = 'response'
                        historical = next((x for x in self.history if x['id'] == message['id']), None)
                        if historical is not None:
                            was_timeout = historical['outcome'] == 'timeout'
                            historical.update(outcome='error' if 'error' in message else 'ok',
                                              received_at=self._now(), late=was_timeout)
                            if was_timeout:
                                self.late_responses += 1
                        waiter = self.pending.get(message['id'])
                        if waiter:
                            waiter['response'] = message
                            waiter['done'].set()
                    elif 'method' in message:
                        key = message.get('sessionId', 'default')
                        method = message['method']
                        if method == 'Debugger.paused':
                            self.protocol[key] = 'paused'
                        elif method == 'Debugger.resumed':
                            self.protocol[key] = 'running'
                        self.activity['last_event'] = method
                        self.sequence += 1
                        if len(raw) > 32768:
                            message = {'method': message['method'], 'truncated': True, 'original_bytes': len(raw)}
                        self.event_buffer.append({'seq': self.sequence, **message})
        except Exception as exc:
            error = type(exc).__name__ + ': debugger connection ended'
        finally:
            try:
                ws.close()
            except Exception:
                pass
            with self.lock:
                if self.ws is ws:
                    self.ws = None
                    self.last_error = error
                    self.last_disconnect = {
                        'time': self._now(), 'error': error,
                        'code': getattr(ws, 'close_code', None),
                        'reason': str(getattr(ws, 'close_reason', '') or '')[:300],
                    }
                for waiter in self.pending.values():
                    if not waiter['done'].is_set():
                        entry = next((x for x in self.history if x['outcome'] == 'pending' and self.pending.get(x['id']) is waiter), None)
                        if entry is not None:
                            entry['outcome'] = 'disconnected'
                        waiter['error'] = error
                        waiter['done'].set()

    def _command(self, method, params=None, seconds=10, session_id=None):
        seconds = timeout(seconds)
        if not isinstance(method, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9]*', method):
            raise ValueError('method must be Domain.command')
        if params is None: params = {}
        if not isinstance(params, dict): raise ValueError('params must be an object')
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            raise ValueError('session_id must be a nonempty CDP session string')
        waiter = {'done': threading.Event()}
        with self.send_lock:
            with self.lock:
                if self.ws is None: raise ValueError('Connect to a debugger first')
                ws = self.ws
                self.request_id += 1
                rid = self.request_id
                self.pending[rid] = waiter
                record = {'id': rid, 'method': method, 'session_id': session_id,
                          'sent_at': self._now(), 'outcome': 'pending'}
                self.history.append(record)
                self.activity['last_send'] = record['sent_at']
            message = {'id': rid, 'method': method, 'params': params}
            if session_id is not None: message['sessionId'] = session_id
            try:
                ws.send(json.dumps(message, allow_nan=False))
            except Exception:
                with self.lock:
                    self.pending.pop(rid, None)
                    record['outcome'] = 'send_failed'
                raise RuntimeError('CDP send failed; outcome may be unknown, inspect before retry') from None
        try:
            if not waiter['done'].wait(seconds):
                with self.lock:
                    # A response arriving at the deadline must not be reported as a timeout.
                    expired = not waiter['done'].is_set()
                    if expired:
                        self.pending.pop(rid, None)
                        record['outcome'] = 'timeout'
                        self.timed_out += 1
                        self.last_outcome = 'timeout'
                if expired:
                    self.context.logger.warning('Debugger command timed out', method=method, request_id=rid)
                    raise TimeoutError(f'CDP {method} timed out; outcome unknown, no automatic retry')
            if 'error' in waiter: raise RuntimeError(waiter['error'] + '; outcome may be unknown')
            response = waiter['response']
            if 'error' in response:
                raise RuntimeError('CDP error: ' + json.dumps(response['error'], ensure_ascii=False))
            return response.get('result', {})
        finally:
            with self.lock: self.pending.pop(rid, None)

    def call(self, action, arguments):
        a = dict(arguments)
        allowed = {
            'targets': {'endpoint', 'timeout'}, 'connect': {'url', 'timeout', 'heartbeat'},
            'prepare': {'release_wait', 'timeout', 'session_id'},
            'command': {'method', 'params', 'timeout', 'session_id'},
            'evaluate': {'expression', 'await_promise', 'return_by_value', 'context_id', 'timeout', 'session_id'},
            'events': {'after', 'limit'}, 'screenshot': {'timeout', 'session_id'},
            'status': set(), 'disconnect': set()
        }
        if action not in allowed or set(a) - allowed[action]: raise ValueError('Unknown action or parameters')
        if action == 'prepare':
            return self._prepare(a)
        if action == 'targets':
            endpoint = a.get('endpoint', 'http://127.0.0.1:9222')
            parsed = urlparse(endpoint)
            if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
                raise ValueError('endpoint must be a loopback HTTP origin')
            opener = build_opener(ProxyHandler({}), NoRedirect())
            with opener.open(endpoint.rstrip('/')+'/json/list', timeout=timeout(a.get('timeout',10))) as response:
                raw = response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024: raise ValueError('Target list too large')
            targets=json.loads(raw)
            if not isinstance(targets,list): raise ValueError('Debugger did not return a target list')
            return {'targets':targets}
        if action == 'connect':
            url=websocket_url(a.get('url'))
            seconds=timeout(a.get('timeout',10))
            heartbeat = a.get('heartbeat', False)
            if type(heartbeat) is not bool:
                raise ValueError('heartbeat must be boolean')
            if heartbeat and 'ping_interval' not in inspect.signature(connect).parameters:
                raise ValueError('Heartbeat needs a websockets version supporting ping_interval')
            self.close()
            options=dict(open_timeout=seconds, close_timeout=1, max_size=8*1024*1024)
            if 'proxy' in inspect.signature(connect).parameters: options['proxy']=None
            if 'ping_interval' in inspect.signature(connect).parameters:
                options['ping_interval'] = 20 if heartbeat else None
            ws=connect(url,**options)
            with self.lock:
                self.event_buffer.clear()
                start_cursor = self.sequence
                self.protocol.clear()
                self.activity.clear()
                self.history.clear()
                self.response_count = self.timed_out = self.late_responses = 0
                self.last_outcome = None
                self.heartbeat = heartbeat
            self.ws,self.url,self.last_error=ws,url,None
            self.reader=threading.Thread(target=self._receive,args=(ws,),daemon=True,name='cdp-events')
            self.reader.start()
            self.context.logger.info('Debugger connected')
            return {'connected':True,'url':url,'cursor':start_cursor}
        if action == 'status':
            return self._status()
        if action == 'disconnect':
            self.close()
            return {'connected':False}
        if action == 'events':
            after,limit=a.get('after',0),a.get('limit',50)
            if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100:
                raise ValueError('after >= 0 and limit 1..100 must be integers')
            with self.lock:
                rows=[e for e in self.event_buffer if e['seq']>after][:limit]
                oldest=self.event_buffer[0]['seq'] if self.event_buffer else self.sequence+1
            return {'events':rows,'cursor':rows[-1]['seq'] if rows else after,'dropped':after<oldest-1}
        seconds=a.get('timeout',10)
        session_id=a.get('session_id')
        if action == 'command':
            return self._command(a.get('method'),a.get('params'),seconds,session_id)
        if action == 'evaluate':
            expression=a.get('expression')
            if not isinstance(expression,str) or not expression or len(expression)>262144:
                raise ValueError('expression must be a nonempty string up to 262144 characters')
            params={'expression':expression,'awaitPromise':a.get('await_promise',True),'returnByValue':a.get('return_by_value',True)}
            if type(params['awaitPromise']) is not bool or type(params['returnByValue']) is not bool:
                raise ValueError('await_promise and return_by_value must be booleans')
            if 'context_id' in a:
                if type(a['context_id']) is not int: raise ValueError('context_id must be an integer')
                params['contextId']=a['context_id']
            return self._command('Runtime.evaluate',params,seconds,session_id)
        result=self._command('Page.captureScreenshot',{'format':'png'},seconds,session_id)
        raw=base64.b64decode(result['data'],validate=True)
        if not raw.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('Debugger returned invalid PNG')
        artifact=save_artifact(raw,'devtools.png','image/png')
        self.context.logger.info('Debugger screenshot saved',bytes=len(raw))
        return {'artifact':artifact}

    def close(self):
        ws=self.ws
        if ws is not None: ws.close()
        if self.reader is not None:
            self.reader.join(3)
            if self.reader.is_alive(): raise RuntimeError('Debugger reader shutdown not confirmed')
        self.ws=None
        self.reader=None


def create(context):
    return DevTools(context)
