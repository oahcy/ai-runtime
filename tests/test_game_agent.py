import concurrent.futures
import json
from pathlib import Path
import time
import shutil
import subprocess
from urllib.request import urlopen
from urllib.error import HTTPError

import pytest
from websockets.sync.client import connect
from ai_runtime.plugins import Catalog, PluginContext, PluginManager


@pytest.fixture
def agent(tmp_path):
    root = Path(__file__).resolve().parents[1]
    manager = PluginManager(Catalog(tmp_path / 'data'),
                            PluginContext(1, 'agent-test', tmp_path / 'session', tmp_path))
    manager.add(root / 'plugins/game-agent')
    handle = manager.load('game-agent')
    yield handle
    manager.close()


def pairing(agent):
    pair = agent.call('start', {})
    url = pair['url']
    return pair, url.split('/agent.js')[0].replace('http:', 'ws:') + '/ws', url.split('token=')[1]


def test_pair_rotation_origin_and_cleanup(agent):
    pair, wsurl, token = pairing(agent)
    assert "require('./ai-runtime-agent.js')" in pair['loader']
    with urlopen(pair['url']) as response:
        assert 'module.exports' in response.read().decode()
        assert response.headers['Cache-Control'] == 'no-store'
    with pytest.raises(HTTPError) as exc:
        urlopen(pair['url'].split('?')[0])
    assert exc.value.code == 403
    fresh = agent.call('pair', {})
    with pytest.raises(HTTPError):
        urlopen(pair['url'])
    from websockets.exceptions import ConnectionClosed
    with connect(wsurl, origin='https://servicewechat.com') as ws:
        ws.send(json.dumps(dict(type='hello', token=token)))
        with pytest.raises(ConnectionClosed) as closed:
            ws.recv()
        assert closed.value.rcvd.code == 1008
    with connect(wsurl, origin='https://servicewechat.com') as ws:
        ws.send(json.dumps(dict(type='hello', token=fresh['config']['token'])))
        assert json.loads(ws.recv())['type'] == 'welcome'
    events = agent.call('events', {})['events']
    assert any(e.get('reason') == 'invalid_or_expired_pairing' for e in events)
    agent.call('stop', {})
    assert not agent.call('status', {})['running']
    # A stopped listener can be started again with fresh credentials.
    assert agent.call('start', {})['url'] != pair['url']


def test_evaluate_timeout_late_reply_logs_disconnect(agent):
    _, url, token = pairing(agent)
    with connect(url) as ws, concurrent.futures.ThreadPoolExecutor() as pool:
        ws.send(json.dumps(dict(type='hello', token=token)))
        target = json.loads(ws.recv())['target_id']
        future = pool.submit(agent.call, 'operate', dict(target_id=target, command={'op': 'roots'}))
        command = json.loads(ws.recv())
        ws.send(json.dumps(dict(type='result', id=command['id'], value=42)))
        assert future.result(3) == {'value': 42}
        slow = pool.submit(agent.call, 'operate', dict(target_id=target, command={'op': 'roots'}, timeout=.1))
        old = json.loads(ws.recv())
        with pytest.raises(TimeoutError, match='unknown'):
            slow.result(3)
        next_call = pool.submit(agent.call, 'operate', dict(target_id=target, command={'op': 'roots'}))
        new = json.loads(ws.recv())
        ws.send(json.dumps(dict(type='result', id=old['id'], value='late')))
        ws.send(json.dumps(dict(type='result', id=new['id'], value=3)))
        assert next_call.result(3) == {'value': 3}
        ws.send(json.dumps(dict(type='log', message='hello')))
        deadline = time.monotonic() + 2
        while not any(e['kind'] == 'log' for e in agent.call('events', {})['events']) and time.monotonic() < deadline:
            time.sleep(.01)
        assert any(e.get('message') == 'hello' for e in agent.call('events', {})['events'])
        pending = pool.submit(agent.call, 'operate', dict(target_id=target, command={'op': 'roots'}))
        ws.recv()
        ws.close()
        with pytest.raises(RuntimeError, match='disconnected'):
            pending.result(3)


def test_validation(agent):
    with pytest.raises(ValueError):
        agent.call('start', {'host': '0.0.0.0'})
    with pytest.raises(ValueError):
        agent.call('start', {'host': '8.8.8.8'})
    with pytest.raises(ValueError):
        agent.call('operate', {'target_id': 'x', 'command': {'op': 'roots'}, 'timeout': float('nan')})


def test_game_agent_javascript():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for agent JavaScript test')
    subprocess.run([node, str(Path(__file__).with_name('game_agent_harness.js'))],
                   check=True, capture_output=True, text=True, timeout=10)
