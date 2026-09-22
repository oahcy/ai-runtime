import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
import websockets
from fastapi.testclient import TestClient

from ai_runtime.api import create_app
from ai_runtime.plugins import Catalog, PluginContext, PluginManager, read_manifest, scaffold

EFFECTS = Path(__file__).resolve().parents[1] / 'plugins' / 'effects'


def manager(tmp_path, name='session'):
    return PluginManager(Catalog(tmp_path / 'data'), PluginContext(1, name, tmp_path / name, tmp_path))


def test_register_is_metadata_only_and_conflicts(tmp_path):
    folder = Path(scaffold(tmp_path / 'one', 'demo'))
    with (folder / 'plugin.py').open('a') as f:
        f.write('\nraise RuntimeError("must not import at registration")\n')
    catalog = Catalog(tmp_path / 'data')
    row = catalog.add(folder)
    assert catalog.add(folder) == row
    assert Catalog(tmp_path / 'data').get('demo') == row
    second = Path(scaffold(tmp_path / 'two', 'demo'))
    with pytest.raises(ValueError, match='already registered'):
        catalog.add(second)
    assert len(catalog.list()) == 1
    with pytest.raises(RuntimeError, match='must not import'):
        manager(tmp_path).load('demo')


def test_manifest_entry_and_api_validation(tmp_path):
    folder = Path(scaffold(tmp_path / 'demo', 'demo'))
    manifest = read_manifest(folder)
    (tmp_path / 'outside.py').write_text('')
    for changes in ({'entry': '../outside.py'}, {'api_version': 2}, {'api_version': True}, {'id': '../oops'}):
        (folder / 'plugin.json').write_text(json.dumps(dict(manifest, **changes)))
        with pytest.raises(ValueError):
            Catalog(tmp_path / 'data').add(folder)


def test_instances_context_unload_and_closed_handle(tmp_path):
    folder = Path(scaffold(tmp_path / 'demo', 'demo'))
    a, b = manager(tmp_path, 'a'), manager(tmp_path, 'b')
    a.add(folder)
    x, y = a.load('demo'), b.load('demo')
    assert a.load('demo') is x
    assert x is not y
    assert x.call('ping')['session_id'] == 'a'
    assert y.call('ping')['session_id'] == 'b'
    with pytest.raises(ValueError):
        x.call('unknown')
    assert a.unload('demo')
    assert not a.unload('demo')
    with pytest.raises(ValueError, match='closed'):
        x.call('ping')
    assert a.load('demo') is not x
    a.close(); b.close()


async def test_effects_real_preview_and_cleanup(tmp_path):
    m = manager(tmp_path)
    m.add(EFFECTS)
    fx = m.load('effects')
    urls = []
    try:
        for scene in ('neon', 'wind'):
            info = fx.call('start', {'scene': scene})
            urls.append(info['url'].split('#')[0])
            base, token = info['url'].split('#')
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(base)
                assert response.status_code == 200
                assert ('NEON' if scene == 'neon' else 'FLOW') in response.text
            async with websockets.connect(base.replace('http:', 'ws:')+'ws', origin=base.rstrip('/')) as ws:
                await ws.send(json.dumps({'token': token}))
                first = json.loads(await asyncio.wait_for(ws.recv(), 2))
                if scene == 'neon':
                    fx.call('configure', {'scene': scene, 'changes': {'hue': 280}, 'message': 'test purple'})
                    changed = json.loads(await asyncio.wait_for(ws.recv(), 2))
                    assert changed['state']['hue'] == 280
                else:
                    while True:
                        changed = json.loads(await asyncio.wait_for(ws.recv(), 2))
                        if changed['state']['tick'] > first['state']['tick']:
                            break
                    assert changed['state']['points'] != first['state']['points']
        objects = list(fx._instance.scenes.values())
        m.unload('effects')
        assert all(obj._thread is None for obj in objects)
        assert all(not getattr(obj, '_physics', None) or not obj._physics.is_alive() for obj in objects)
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            for url in urls:
                with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
                    await client.get(url)
    finally:
        m.close()


def test_http_plugin_jobs_in_kernel_and_dedup(tmp_path):
    folder = Path(scaffold(tmp_path / 'demo', 'demo'))
    with TestClient(create_app(tmp_path / 'data', 'test-token')) as client:
        assert client.get('/plugins').status_code == 401
        client.headers['Authorization'] = 'Bearer test-token'
        assert client.get('/plugins').json() == {'plugins': []}
        assert client.post('/plugins', json={'path': str(folder)}).status_code == 200
        assert client.get('/plugins/demo').json()['manifest']['api_version'] == 1
        sid = client.post('/sessions', json={'cwd': str(tmp_path)}).json()['id']
        def job(route, body):
            response = client.post(f'/sessions/{sid}/plugins/demo/{route}', json=body)
            assert response.status_code == 202, response.text
            eid = response.json()['id']
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                row = client.get('/executions/'+eid).json()
                if row['status'] not in ('queued', 'running'):
                    assert row['status'] == 'succeeded', row
                    return eid
                time.sleep(.05)
            raise AssertionError('plugin execution did not complete')
        job('load', {'request_id': 'load'})
        payload = {'request_id': 'ping', 'action': 'ping', 'arguments': {}}
        eid = job('call', payload)
        assert job('call', payload) == eid
        events = client.get(f'/sessions/{sid}/events').json()['events']
        assert any(sid in e['payload'].get('text', '') and '"ok": true' in e['payload'].get('text', '') for e in events)
        job('unload', {'request_id': 'unload'})
        client.delete('/sessions/'+sid)
