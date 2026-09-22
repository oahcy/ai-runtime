import asyncio
import json
import math

import httpx
import pytest
import websockets

from ai_runtime.preview import Preview


def test_json_state_is_atomic_and_detached():
    value = {'items': [1]}
    p = Preview(state={'nested': value})
    value['items'].append(2)
    assert p.snapshot()['state']['nested']['items'] == [1]
    before = p.snapshot()
    with pytest.raises(ValueError):
        p.update(valid=3, invalid=math.nan)
    assert p.snapshot() == before
    p.update(nested=value)
    value['items'].append(3)
    snap = p.snapshot()
    assert snap['state']['nested']['items'] == [1, 2]
    snap['state']['nested']['items'].append(4)
    assert p.snapshot()['state']['nested']['items'] == [1, 2]
    for i in range(35):
        p.update(counter=i)
    assert len(p.snapshot()['history']) == 30


async def test_real_websocket_updates_reconnect_and_auth():
    p = Preview(html="<h1>Custom preview</h1>", state={"score": 0})
    info = p.start()
    assert p.start() == info
    base, token = info['url'].split('#')
    origin = base.rstrip('/')
    ws_url = base.replace('http:', 'ws:') + 'ws'
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(base)
            assert response.status_code == 200
            assert token not in response.text
            assert '<h1>Custom preview</h1>' in response.text
            assert 'globalThis.RuntimePreview' in response.text
            assert 'no-store' in response.headers['cache-control']
        async with websockets.connect(ws_url, origin=origin) as ws:
            await ws.send(json.dumps({'token': token}))
            first = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert first['revision'] == 0
            p.update(hue=280, particles=900, message='purple')
            changed = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert changed['state']['hue'] == 280
            assert changed['history'][-1]['message'] == 'purple'
            # Browser input is not a mutation or Python execution API.
            await ws.send(json.dumps({'speed': 3}))
        p.update(speed=1.2, message='faster')
        async with websockets.connect(ws_url, origin=origin) as ws:
            await ws.send(json.dumps({'token': token}))
            latest = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert latest['revision'] == 2
            assert latest['state']['speed'] == 1.2
        async with websockets.connect(ws_url, origin=origin) as ws:
            await ws.send(json.dumps({'token': 'wrong'}))
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()
            assert ws.close_code == 1008
        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(ws_url, origin='https://example.com'):
                pass
    finally:
        p.stop()
    assert p.info()['running'] is False
    assert p.info()['url'] is None

def test_publish_replaces_frame_atomically_without_history():
    p = Preview(state={'old': 1})
    value = {'points': [[1, 2]]}
    assert p.publish(value) == 1
    value['points'][0][0] = 9
    assert p.snapshot() == {'revision': 1, 'state': {'points': [[1, 2]]}, 'history': []}
    with pytest.raises(ValueError):
        p.publish({'invalid': math.nan})
    assert p.snapshot()['revision'] == 1
    p.update(message='changed', label='ok')
    p.publish({'points': []})
    assert p.snapshot()['state'] == {'points': []}
    assert len(p.snapshot()['history']) == 1


def test_browser_subscription_reconnect_and_auth(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path
    from ai_runtime.preview_client import CLIENT_JS
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for browser client unit test')
    harness = Path(__file__).with_name('preview_client_harness.js').read_text(encoding='utf-8')
    script = tmp_path / 'client-test.js'
    script.write_text(harness.replace('/* CLIENT */', CLIENT_JS), encoding='utf-8')
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True, timeout=10)
