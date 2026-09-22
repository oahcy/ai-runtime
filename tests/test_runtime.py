import asyncio
import json
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_runtime.api import create_app
from ai_runtime.engine import Runtime, TERMINAL, Pending
from ai_runtime.store import Store


async def done(rt, eid, timeout=15):
    async with asyncio.timeout(timeout):
        while rt.execution(eid)['status'] not in TERMINAL:
            await asyncio.sleep(0.05)
    return rt.execution(eid)


async def run(rt, sid, code, timeout=10):
    row = rt.submit(sid, uuid.uuid4().hex, code, timeout)
    return await done(rt, row['id'])


@pytest.fixture
async def runtime(tmp_path):
    rt = Runtime(tmp_path / 'data')
    try:
        yield rt
    finally:
        await rt.close()


def test_completion_requires_reply_and_idle():
    p = Pending('one')
    p.reply = {'status': 'ok'}
    p.complete()
    assert not p.done.is_set()
    p.idle = True
    p.complete()
    assert p.done.is_set()


async def test_persistent_variables_queue_dedup_and_error_recovery(runtime, tmp_path):
    rt = runtime
    sid = (await rt.create(tmp_path))['id']
    first = rt.submit(sid, 'first', 'value = 40')
    second = rt.submit(sid, 'second', 'value += 2\nprint(value)')
    assert rt.submit(sid, 'second', 'value += 2\nprint(value)')['id'] == second['id']
    with pytest.raises(ValueError):
        rt.submit(sid, 'second', 'value = 999')
    assert (await done(rt, first['id']))['status'] == 'succeeded'
    assert (await done(rt, second['id']))['status'] == 'succeeded'
    events = rt.store.events(sid, limit=1000)
    assert any(e['kind'] == 'output.stream' and '42' in e['payload']['text'] for e in events)
    assert (await run(rt, sid, 'raise ValueError("test error")'))['status'] == 'failed'
    assert (await run(rt, sid, 'assert value == 42'))['status'] == 'succeeded'
    assert (await run(rt, sid, 'input("cannot block")'))['status'] == 'failed'


async def test_independent_kernels_and_late_output(runtime, tmp_path):
    rt = runtime
    sid = (await rt.create(tmp_path))['id']
    other = (await rt.create(tmp_path))['id']
    await run(rt, sid, 'secret = 123')
    assert (await run(rt, other, 'secret'))['status'] == 'failed'
    row = await run(rt, sid, "import threading, time\ndef later():\n    time.sleep(0.5)\n    print('late-output')\nthreading.Thread(target=later).start()")
    assert row['status'] == 'succeeded'
    await asyncio.sleep(1)
    assert any('late-output' in str(e['payload']) for e in rt.store.events(sid, limit=1000))


async def test_timeout_manual_interrupt_and_cancelled_queue(runtime, tmp_path):
    rt = runtime
    sid = (await rt.create(tmp_path))['id']
    row = await run(rt, sid, 'while True: pass', timeout=0.5)
    assert row['status'] == 'interrupted', row
    assert row['reason'] == 'timeout'
    assert (await run(rt, sid, 'after_timeout = True'))['status'] == 'succeeded'
    active = rt.submit(sid, 'long', 'import time\nwhile True: time.sleep(0.05)', 120)
    queued = rt.submit(sid, 'cancelled', 'should_not_exist = True')
    await rt.interrupt(queued['id'])
    while rt.execution(active['id'])['status'] == 'queued':
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    await rt.interrupt(active['id'])
    assert (await done(rt, active['id']))['status'] == 'interrupted'
    assert (await run(rt, sid, "assert 'should_not_exist' not in globals()"))['status'] == 'succeeded'


async def test_artifact_checkpoint_and_explicit_restart(runtime, tmp_path):
    rt = runtime
    sid = (await rt.create(tmp_path))['id']
    code = "memory = 42\nruntime.checkpoint_save({'step': 2})\nruntime.save_artifact('hello', '../report.txt', 'text/plain')"
    assert (await run(rt, sid, code))['status'] == 'succeeded'
    artifacts = rt.store.all('SELECT * FROM artifacts')
    assert len(artifacts) == 1
    from pathlib import Path
    assert Path(artifacts[0]['path']).read_text() == 'hello'
    with pytest.raises(ValueError):
        rt._artifact(sid, tmp_path / 'outside.txt', 'text/plain')
    restarted = await rt.restart(sid)
    assert restarted['generation'] == 2
    assert (await run(rt, sid, 'memory'))['status'] == 'failed'
    assert (await run(rt, sid, "assert runtime.checkpoint_load() == {'step': 2}"))['status'] == 'succeeded'


async def test_kernel_death_is_unknown_not_success(runtime, tmp_path):
    rt = runtime
    sid = (await rt.create(tmp_path))['id']
    row = await run(rt, sid, 'import os; os._exit(3)')
    assert row['status'] == 'unknown'
    assert rt.session(sid)['status'] == 'lost'
    with pytest.raises(ValueError):
        rt.submit(sid, 'no', 'print("must not execute")')


def test_store_restart_does_not_replay(tmp_path):
    store = Store(tmp_path)
    store.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', ('s', str(tmp_path), 'ready', 1, time.time()))
    store.execute('INSERT INTO executions VALUES(?,?,?,?,?,?,?,?)', ('e', 's', 'r', 'print(1)', 30, 'running', None, time.time()))
    store.close()
    store = Store(tmp_path)
    try:
        assert store.one('SELECT status FROM sessions')['status'] == 'lost'
        assert store.one('SELECT status FROM executions')['status'] == 'unknown'
    finally:
        store.close()


def test_http_auth_reconnect_artifact_and_websocket(tmp_path):
    app = create_app(tmp_path / 'api', 'test-token')
    headers = {'Authorization': 'Bearer test-token'}
    with TestClient(app) as client:
        assert client.get('/sessions').status_code == 401
        assert client.get('/sessions', headers={'Authorization': 'Bearer wrong'}).status_code == 401
        response = client.post('/sessions', json={'cwd': str(tmp_path)}, headers=headers)
        assert response.status_code == 201, response.text
        sid = response.json()['id']
        first = client.post(f'/sessions/{sid}/executions', json={'request_id': 'one', 'code': 'value = 41'}, headers=headers).json()
        second = client.post(f'/sessions/{sid}/executions', json={'request_id': 'two', 'code': "value += 1\nprint(value)\nruntime.save_artifact('hello', 'report.txt', 'text/plain')"}, headers=headers).json()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = client.get('/executions/' + second['id'], headers=headers).json()
            if result['status'] in TERMINAL:
                break
            time.sleep(0.1)
        assert result['status'] == 'succeeded', result
        events = client.get(f'/sessions/{sid}/events', headers=headers).json()
        assert any('42' in str(e['payload']) for e in events['events'])
        aid = next(e['payload']['id'] for e in events['events'] if e['kind'] == 'artifact.created')
        assert client.get('/artifacts/' + aid, headers=headers).text == 'hello'
        cursor = events['events'][0]['seq']
        with client.websocket_connect(f'/sessions/{sid}/events/ws?after={cursor}', headers=headers) as ws:
            event = ws.receive_json()
            assert event['seq'] > cursor
        client.delete('/sessions/' + sid, headers=headers)


async def test_unconfirmed_interrupt_blocks_following_work(runtime, tmp_path):
    rt = runtime
    sid = (await rt.create(tmp_path))['id']
    # Exercise a kernel that cannot acknowledge the interrupt within the grace period.
    async def ignored_interrupt():
        pass
    rt.sessions[sid].manager.interrupt_kernel = ignored_interrupt
    active = rt.submit(sid, 'blocked', 'import time; time.sleep(60)', 0.3)
    queued = rt.submit(sid, 'behind', 'must_not_run = True')
    assert (await done(rt, active['id']))['status'] == 'unknown'
    assert (await done(rt, queued['id']))['status'] == 'cancelled'
    assert rt.session(sid)['status'] == 'lost'


def test_data_directory_has_single_owner(tmp_path):
    first = Store(tmp_path)
    try:
        with pytest.raises(RuntimeError, match='another runtime'):
            Store(tmp_path)
    finally:
        first.close()
