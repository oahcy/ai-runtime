import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ai_runtime.api import create_app


async def test_hosted_preview_real_kernel(tmp_path):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    app = create_app(tmp_path / 'data', 'api-secret')
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(.02)
        async with httpx.AsyncClient(base_url=base, headers={'Authorization': 'Bearer api-secret'}, timeout=40) as client:
            sid = (await client.post('/sessions', json={'cwd': str(tmp_path)})).json()['id']

            async def execute(code):
                job = (await client.post(f'/sessions/{sid}/executions', json={
                    'request_id': str(__import__('uuid').uuid4()), 'code': code})).json()
                async with asyncio.timeout(30):
                    while True:
                        row = (await client.get('/executions/' + job['id'])).json()
                        if row['status'] not in ('queued', 'running'):
                            assert row['status'] == 'succeeded', row
                            return
                        await asyncio.sleep(.05)

            await execute("from ai_runtime.preview import Preview\np = Preview(state={'x': 1})\nq = Preview(state={'x': 2})\np.start()\nq.start()\nassert p._socket is None and q._socket is None\nassert p.info()['mode'] == 'hosted'")
            hub = app.state.preview_hub
            await execute(f"assert p.info()['url'].startswith({base!r} + '/previews/')")
            assert len(hub.views) == 2
            pid = next(pid for pid, row in hub.views.items() if row['snapshot']['state']['x'] == 1)
            path = f'/previews/{pid}/'
            token = hub.views[pid]['token']
            # Viewer page contains no API, producer, or viewer credentials.
            page = await client.get(path, headers={'Authorization': ''})
            assert page.status_code == 200
            for secret in ('api-secret', hub.sessions[sid], token):
                assert secret not in page.text
            assert 'RuntimePreview' in page.text
            assert page.headers['cache-control'] == 'no-store'
            assert (await client.get(path.rstrip('/'))).status_code == 307
            wsurl = base.replace('http:', 'ws:') + path + 'ws'
            async with connect(wsurl, origin=base) as ws:
                await ws.send(json.dumps({'token': 'bad'}))
                with pytest.raises(ConnectionClosed):
                    await ws.recv()
                assert ws.close_code == 1008
            with pytest.raises(InvalidStatus):
                async with connect(wsurl, origin='http://evil.invalid'):
                    pass
            async with connect(wsurl, origin=base) as ws:
                await ws.send(json.dumps({'token': token}))
                assert json.loads(await ws.recv())['state'] == {'x': 1}
                await execute("p.publish({'x': 7})")
                assert json.loads(await asyncio.wait_for(ws.recv(), 5))['state'] == {'x': 7}
            async with connect(wsurl, origin=base) as ws:
                await ws.send(json.dumps({'token': token}))
                assert json.loads(await ws.recv())['state'] == {'x': 7}
            await execute('p.stop()')
            assert (await client.get(path)).status_code == 404
            assert len(hub.views) == 1
            oldkey = hub.sessions[sid]
            assert (await client.post(f'/sessions/{sid}/restart')).status_code == 200
            assert not hub.views and hub.sessions[sid] != oldkey
            with pytest.raises(InvalidStatus):
                async with connect(base.replace('http:', 'ws:') + f'/_internal/previews/{sid}/' + 'a'*32,
                                   additional_headers={'Authorization': 'Bearer ' + oldkey}):
                    pass
            await execute("from ai_runtime.preview import Preview\np=Preview()\np.start()")
            assert len(hub.views) == 1
            effects = Path(__file__).resolve().parents[1] / 'plugins' / 'effects'
            response = await client.post('/plugins', json={'path': str(effects)})
            assert response.status_code in (200, 201), response.text
            await execute("fx = runtime.plugins.load('effects')\nfx.call('start', {'scene': 'wind', 'count': 100})")
            assert len(hub.views) == 2
            await execute("runtime.plugins.unload('effects')\nassert runtime.plugins.cleanup_report('effects')['ok']")
            assert len(hub.views) == 1
            await app.state.runtime.sessions[sid].manager.shutdown_kernel(now=True)
            async with asyncio.timeout(5):
                while hub.views or sid in hub.sessions:
                    await asyncio.sleep(.05)
            await client.post(f'/sessions/{sid}/restart')
            await execute("from ai_runtime.preview import Preview\np=Preview()\np.start()")
            assert len(hub.views) == 1
            await client.delete(f'/sessions/{sid}')
            assert not hub.views and sid not in hub.sessions
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 20)
        sock.close()

