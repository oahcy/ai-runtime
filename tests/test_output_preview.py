import asyncio
import json
import socket
import uuid

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from ai_runtime.api import create_app
from ai_runtime.output_preview import OutputBuffer, MAX_BYTES

PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII='


def display(text, did='same'):
    return {'data': {'text/html': text}, 'transient': {'display_id': did}}


def test_projection_updates_clear_and_bounds():
    b = OutputBuffer()
    b.apply('a', 'display_data', display('one'))
    b.apply('b', 'display_data', display('two'))
    b.apply('c', 'update_display_data', display('changed'))
    assert [x['payload']['data']['text/html'] for x in b.items] == ['changed', 'changed']
    b.apply('a', 'clear_output', {'wait': True})
    assert len(b.items) == 2
    b.apply('a', 'stream', {'name': 'stdout', 'text': 'replacement'})
    assert [x['area'] for x in b.items] == ['b', 'a']
    b.apply('a', 'clear_output', {'wait': False})
    assert [x['area'] for x in b.items] == ['b']
    b.apply('b', 'clear_output', {})
    b.apply('b', 'update_display_data', display('must not reappear'))
    assert b.items == []
    payload = display('original')
    b.apply('a', 'display_data', payload)
    payload['data']['text/html'] = 'mutated'
    assert b.items[0]['payload']['data']['text/html'] == 'original'
    b.apply('a', 'display_data', display('x' * MAX_BYTES))
    assert b.dropped == 1 and len(json.dumps(b.state()).encode()) < MAX_BYTES
    for i in range(210):
        b.apply(str(i), 'stream', {'name': 'stdout', 'text': str(i)})
    assert len(b.items) == 200


async def test_live_kernel_output_bridge(tmp_path):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    base = 'http://127.0.0.1:' + str(sock.getsockname()[1])
    app = create_app(tmp_path / 'data', 'secret')
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(.02)
        async with httpx.AsyncClient(base_url=base, headers={'Authorization': 'Bearer secret'}, timeout=40) as client:
            sid = (await client.post('/sessions', json={'cwd': str(tmp_path)})).json()['id']
            async def execute(code):
                job = (await client.post(f'/sessions/{sid}/executions', json={'request_id': uuid.uuid4().hex, 'code': code})).json()
                result = (await client.get(f"/executions/{job['id']}/result", params={'wait_seconds': 20})).json()
                assert result['done'] and result['status'] == 'succeeded', result
                return result
            # Rich display automatically creates a viewer, without asking kernel code to host HTML.
            await execute(f"import base64\nfrom IPython.display import display, Image, HTML, clear_output\nh=display(Image(data=base64.b64decode({PNG!r})), display_id=True)")
            views = (await client.get('/previews')).json()['previews']
            assert len(views) == 1 and views[0]['name'] == '内核输出'
            assert (await client.post(f'/sessions/{sid}/output-preview', headers={'Authorization': ''})).status_code == 401
            view = (await client.post(f'/sessions/{sid}/output-preview')).json()
            assert view['id'] == views[0]['id']
            path = f"/previews/{view['id']}/"
            page = await client.get(path)
            assert 'sandbox' in page.text and "frame-src 'self'" in page.headers['content-security-policy']
            assert view['url'].split('#')[1] not in page.text
            wsurl = base.replace('http:', 'ws:') + path + 'ws'
            async with connect(wsurl, origin=base) as ws:
                await ws.send(json.dumps({'token': 'wrong'}))
                with pytest.raises(ConnectionClosed): await ws.recv()
            async with connect(wsurl, origin=base) as ws:
                await ws.send(json.dumps({'token': view['url'].split('#')[1]}))
                first = json.loads(await ws.recv())
                assert first['state']['items'][0]['payload']['data']['image/png'] == PNG
                # Removing persisted artifacts does not affect the in-memory viewer image.
                for row in app.state.runtime.store.all('SELECT path FROM artifacts WHERE session_id=?', (sid,)):
                    from pathlib import Path
                    Path(row['path']).unlink()
                await execute("h.update(HTML('<b>updated</b>'))")
                async with asyncio.timeout(5):
                    while True:
                        changed = json.loads(await ws.recv())
                        if changed['state']['items'][0]['payload']['data'].get('text/html') == '<b>updated</b>': break
                assert len(changed['state']['items']) == 1
                # clear_output is scoped to the current execution, not unrelated prior cells.
                await execute("display(HTML('old')); clear_output(wait=True); display(HTML('new'))")
            async with connect(wsurl, origin=base) as ws:
                await ws.send(json.dumps({'token': view['url'].split('#')[1]}))
                items = json.loads(await ws.recv())['state']['items']
                assert [i['payload']['data'].get('text/html') for i in items] == ['<b>updated</b>', 'new']
            await client.post(f'/sessions/{sid}/restart')
            assert (await client.get(path)).status_code == 404
            assert not app.state.preview_hub.outputs
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 20)
        sock.close()


def test_output_renderer():
    import re
    import shutil
    import subprocess
    from pathlib import Path
    from ai_runtime.output_preview import OUTPUT_HTML
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for output renderer test')
    script = re.search(r'<script>([\s\S]*)</script>', OUTPUT_HTML).group(1)
    subprocess.run([node, str(Path(__file__).with_name('output_preview_harness.js'))],
                   input=script, text=True, encoding='utf-8',
                   capture_output=True, check=True, timeout=10)
