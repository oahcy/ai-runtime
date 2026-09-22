"""Run the real CLI/server on a temporary loopback port; leave no service running."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import psutil
import time
from pathlib import Path
from urllib.request import urlopen
from ai_runtime.client import Client


def main():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    with tempfile.TemporaryDirectory(prefix='ai-runtime-smoke-') as folder:
        root = Path(folder)
        with (root / 'server.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-m', 'ai_runtime', '--data-dir', str(root), '--port', str(port)],
                stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            client = None
            sid = None
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError((root / 'server.log').read_text())
                    try:
                        with urlopen(url + '/health', timeout=1) as response:
                            assert json.load(response)['status'] == 'ok'
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise TimeoutError('server did not start')
                token = (root / 'token').read_text().strip()
                client = Client(token, url)
                sid = client.create_session(root)['id']
                assert client.wait(client.execute(sid, 'answer = 40')['id'])['status'] == 'succeeded'
                # Reconnect from a fresh client, preserving the kernel's memory.
                client = Client(token, url)
                result = client.wait(client.execute(sid, "answer += 2\nprint(answer)\nruntime.save_artifact('answer=42', 'result.txt', 'text/plain')")['id'])
                assert result['status'] == 'succeeded'
                events = client.events(sid)['events']
                assert any('42' in str(e['payload']) for e in events)
                assert any(e['kind'] == 'artifact.created' for e in events)
                # Actual WebSocket upgrade and replay, not an in-process test transport.
                from websockets.sync.client import connect
                with connect(f'ws://127.0.0.1:{port}/sessions/{sid}/events/ws',
                             additional_headers={'Authorization': 'Bearer ' + token}) as ws:
                    event = json.loads(ws.recv(timeout=5))
                    assert event['session_id'] == sid
                print(json.dumps({'http': 'ok', 'websocket': 'ok', 'persistent_value': 42,
                                  'artifact': 'saved', 'server_cleanup': 'scheduled'}))
            finally:
                try:
                    if client and sid:
                        client.request('DELETE', '/sessions/' + sid)
                finally:
                    # Windows venv python.exe can be a launcher with a real Python child.
                    try:
                        descendants = psutil.Process(process.pid).children(recursive=True)
                    except psutil.NoSuchProcess:
                        descendants = []
                    for child in reversed(descendants):
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    _, alive = psutil.wait_procs(descendants, timeout=10)
                    for child in alive:
                        child.kill()
                    psutil.wait_procs(alive, timeout=5)
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=10)
    print('Server and kernel stopped; temporary directory released.')


if __name__ == '__main__':
    main()
