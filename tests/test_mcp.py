import asyncio
import base64
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest
import uvicorn

pytest.importorskip('mcp')
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from ai_runtime.api import create_app
from ai_runtime.mcp_server import Bridge, create_server

PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII='


@pytest.mark.parametrize('url', ['https://example.com', 'http://192.168.1.2:8765', 'http://user:secret@localhost', 'http://localhost/proxy', 'http://localhost?token=secret'])
def test_only_loopback_origin(url, tmp_path):
    with pytest.raises(ValueError):
        Bridge(url, tmp_path / 'token')


async def test_bridge_auth_error_and_image(tmp_path):
    token = tmp_path / 'token'
    token.write_text('private-test-token')
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers['authorization'] == 'Bearer private-test-token'
        if request.url.path.startswith('/artifacts/'):
            return httpx.Response(200, content=base64.b64decode(PNG), headers={'content-type': 'image/png'})
        return httpx.Response(401, json={'detail': 'must never leak private-test-token'})
    bridge = Bridge('http://127.0.0.1:8765', token, httpx.MockTransport(handler))
    with pytest.raises(ToolError) as error:
        await bridge.request('GET', '/sessions')
    assert 'private-test-token' not in str(error.value)
    assert len(requests) == 1
    result = await bridge.artifact('a' * 32)
    assert result.content[0].type == 'image'
    assert result.content[0].data == PNG
    server = create_server(bridge)
    tools = await server.list_tools()
    assert len(tools) == 12
    by_name = {tool.name: tool for tool in tools}
    assert by_name['execute'].annotations.readOnlyHint is False
    assert by_name['read_artifact'].annotations.readOnlyHint is True
    assert 'request_id' in by_name['execute'].inputSchema['required']


async def test_artifact_limit_and_no_write_retries(tmp_path, monkeypatch):
    token = tmp_path / 'token'
    token.write_text('secret')
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == 'POST':
            raise httpx.ReadTimeout('test', request=request)
        return httpx.Response(200, content=b'12345', headers={'content-type': 'application/octet-stream'})
    bridge = Bridge('http://localhost:8765', token, httpx.MockTransport(handler))
    with pytest.raises(ToolError, match='outcome may be unknown'):
        await bridge.request('POST', '/sessions/x/executions', {'code': 'side_effect()'})
    assert len(calls) == 1
    monkeypatch.setattr('ai_runtime.mcp_server.MAX_ARTIFACT_BYTES', 4)
    with pytest.raises(ToolError, match='8 MiB'):
        await bridge.artifact('a' * 32)


def unpack(result):
    assert not result.isError, result
    return result.structuredContent or json.loads(result.content[0].text)


async def test_real_stdio_to_http_to_kernel_and_reconnect(tmp_path):
    token = tmp_path / 'token'
    token.write_text('integration-token')
    app = create_app(tmp_path / 'runtime', 'integration-token')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    parameters = StdioServerParameters(command=sys.executable, args=[
        '-m', 'ai_runtime.mcp_server', '--runtime-url', f'http://127.0.0.1:{port}', '--token-file', str(token)])
    sid = None
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.05)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                assert len((await client.list_tools()).tools) == 12
                sid = unpack(await client.call_tool('create_session', {'cwd': str(tmp_path)}))['id']
                job = unpack(await client.call_tool('execute', {'session_id': sid, 'request_id': 'first', 'code': 'answer = 40'}))
                assert unpack(await client.call_tool('wait_execution', {'execution_id': job['id']}))['status'] == 'succeeded'
        # A new MCP process must reuse daemon-owned state rather than create another kernel.
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                code = "answer += 2\nprint(answer)\nimport base64\nruntime.save_artifact(base64.b64decode(" + repr(PNG) + "), 'screen.png', 'image/png')"
                arguments = {'session_id': sid, 'request_id': 'second', 'code': code}
                job = unpack(await client.call_tool('execute', arguments))
                assert unpack(await client.call_tool('execute', arguments))['id'] == job['id']
                assert unpack(await client.call_tool('wait_execution', {'execution_id': job['id']}))['status'] == 'succeeded'
                events = unpack(await client.call_tool('read_events', {'session_id': sid}))
                assert any('42' in str(event['payload']) for event in events['events'])
                aid = next(e['payload']['id'] for e in events['events'] if e['kind'] == 'artifact.created')
                result = await client.call_tool('read_artifact', {'artifact_id': aid})
                assert not result.isError
                assert result.content[0].type == 'image'
                assert result.content[0].data == PNG
                # Invalid identifiers cannot be turned into arbitrary HTTP paths.
                bad = await client.call_tool('get_session', {'session_id': '../health'})
                assert bad.isError
                await client.call_tool('close_session', {'session_id': sid})
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 20)
        sock.close()
