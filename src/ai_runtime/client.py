"""Minimal synchronous Python client. Event WebSocket is also available in the API."""
import json
import time
import uuid
from urllib.request import Request, urlopen


class Client:
    def __init__(self, token: str, url='http://127.0.0.1:8765'):
        self.token, self.url = token, url.rstrip('/')

    def request(self, method, path, body=None):
        request = Request(self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'}, method=method)
        with urlopen(request, timeout=60) as response:
            return json.load(response)

    def create_session(self, cwd):
        return self.request('POST', '/sessions', {'cwd': str(cwd)})

    def execute(self, sid, code, *, request_id=None, timeout=30):
        return self.request('POST', f'/sessions/{sid}/executions',
            {'request_id': request_id or uuid.uuid4().hex, 'code': code, 'timeout': timeout})

    def wait(self, eid, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.request('GET', f'/executions/{eid}')
            if result['status'] not in ('queued', 'running', 'interrupting'):
                return result
            time.sleep(0.1)
        raise TimeoutError('client stopped waiting; execution may still be running')

    def events(self, sid, after=0):
        return self.request('GET', f'/sessions/{sid}/events?after={after}')
