import asyncio
import json
import math

import httpx
import pytest
import websockets

from ai_runtime.preview import Preview


def test_atomic_validation_and_bounded_history():
    p = Preview()
    for changes in ({'hue': math.nan}, {'particles': 2.5}, {'rings': True},
                    {'unknown': 1}, {'speed': 9}, {'title': 'x' * 81}):
        before = p.snapshot()
        with pytest.raises(ValueError):
            p.update(**changes)
        assert p.snapshot() == before
    with pytest.raises(ValueError):
        p.update(hue=20, speed=-1)
    assert p.snapshot()['state']['hue'] == 190
    for i in range(35):
        p.update(hue=i, message=f'change {i}')
    snap = p.snapshot()
    assert snap['revision'] == 35
    assert len(snap['history']) == 30
    snap['history'][0]['changes']['hue'] = 200
    assert p.snapshot()['history'][0]['changes']['hue'] == 5


async def test_real_websocket_updates_reconnect_and_auth():
    p = Preview()
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
