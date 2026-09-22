"""Local stdio MCP adapter. The separate HTTP daemon owns all kernel state."""
import argparse
import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

TERMINAL = {'succeeded', 'failed', 'interrupted', 'unknown', 'cancelled'}
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
Identifier = Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')]
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


class Bridge:
    def __init__(self, url: str, token_file: Path, transport=None):
        parsed = urlparse(url)
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError('runtime URL must be a loopback HTTP origin without credentials or path')
        self.url, self.token_file, self.transport = url.rstrip('/'), token_file.resolve(), transport

    def client(self):
        try:
            token = self.token_file.read_text(encoding='utf-8').strip()
        except OSError:
            raise ToolError('Cannot read Runtime token file. Start ai-runtime and check --token-file.') from None
        if not token:
            raise ToolError('Runtime token file is empty.')
        return httpx.AsyncClient(base_url=self.url, headers={'Authorization': 'Bearer ' + token},
                                 timeout=45, follow_redirects=False, trust_env=False, transport=self.transport)

    @staticmethod
    def check(response):
        if response.status_code >= 300:
            messages = {401: 'Runtime authentication failed. Check the token file.',
                        404: 'Runtime resource not found.',
                        409: 'Runtime conflict. Check session state and request_id reuse.',
                        422: 'Invalid Runtime request parameters.'}
            raise ToolError(messages.get(response.status_code, f'Runtime HTTP error {response.status_code}.'))

    async def request(self, method, path, body=None):
        try:
            async with self.client() as client:
                response = await client.request(method, path, json=body)
                self.check(response)
                return response.json()
        except httpx.TimeoutException:
            raise ToolError('Runtime request timed out; outcome may be unknown. Inspect state. If resubmitting execute, reuse the identical request_id and arguments.') from None
        except httpx.RequestError:
            raise ToolError('Cannot reach Runtime. Check that the local daemon is running; do not assume an in-flight write failed.') from None

    async def artifact(self, aid):
        try:
            async with self.client() as client:
                async with client.stream('GET', '/artifacts/' + aid) as response:
                    self.check(response)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > MAX_ARTIFACT_BYTES:
                            raise ToolError('Artifact exceeds the 8 MiB MCP limit; use the Runtime HTTP API to download it.')
                        raw.extend(chunk)
                    mime = response.headers.get('content-type', 'application/octet-stream').split(';')[0]
        except httpx.RequestError:
            raise ToolError('Artifact download failed; retry this read after checking Runtime connectivity.') from None
        if mime in ('image/png', 'image/jpeg', 'image/webp', 'image/gif'):
            content = ImageContent(type='image', data=base64.b64encode(raw).decode(), mimeType=mime)
        elif mime.startswith('text/') or mime == 'application/json':
            content = TextContent(type='text', text=bytes(raw).decode('utf-8', errors='replace'))
        else:
            content = EmbeddedResource(type='resource', resource=BlobResourceContents(
                uri='runtime-artifact://' + aid, mimeType=mime, blob=base64.b64encode(raw).decode()))
        return CallToolResult(content=[content])


def create_server(bridge: Bridge):
    server = FastMCP('ai-runtime', instructions=(
        'Execute Python in persistent local IPython sessions. This is trusted local execution, not a sandbox. '
        'Reuse session_id to preserve memory; client/MCP disconnect does not close the session. '
        'Use a stable unique request_id per logical execute request; uncertain retries must reuse identical arguments. '
        'execute returns a job ID, not its output. Use wait_execution/get_execution and read_events, then read_artifact for images. '
        'Never automatically replay unknown executions or restart kernels: inspect external effects first. '
        'Restart discards all Python memory; checkpoints only store explicit JSON. '
        'Plugins are not implemented yet. Use normal Python libraries and the preloaded runtime SDK.'
    ))

    @server.tool(annotations=READ)
    async def list_sessions() -> dict:
        """List Runtime sessions, including availability and kernel generation."""
        return {'sessions': await bridge.request('GET', '/sessions')}

    @server.tool(annotations=WRITE)
    async def create_session(cwd: str) -> dict:
        """Start a new persistent Python kernel in an existing absolute working directory."""
        if not Path(cwd).is_absolute():
            raise ToolError('cwd must be an absolute path on the Runtime host.')
        return await bridge.request('POST', '/sessions', {'cwd': cwd})

    @server.tool(annotations=READ)
    async def get_session(session_id: Identifier) -> dict:
        """Read session status and generation; lost means memory cannot be trusted."""
        return await bridge.request('GET', f'/sessions/{session_id}')

    @server.tool(annotations=WRITE)
    async def execute(session_id: Identifier,
                      request_id: Annotated[str, Field(min_length=1, max_length=128)],
                      code: Annotated[str, Field(min_length=1, max_length=262144)],
                      timeout: Annotated[float, Field(ge=0.1, le=3600)] = 30) -> dict:
        """Queue Python code. Reuse request_id ONLY for identical retries; timeout is kernel execution seconds. Returns execution ID."""
        return await bridge.request('POST', f'/sessions/{session_id}/executions',
                                    {'request_id': request_id, 'code': code, 'timeout': timeout})

    @server.tool(annotations=READ)
    async def get_execution(execution_id: Identifier) -> dict:
        """Read job status. succeeded means code finished, not that the user's goal was achieved."""
        return await bridge.request('GET', f'/executions/{execution_id}')

    @server.tool(annotations=READ)
    async def wait_execution(execution_id: Identifier,
                             wait_seconds: Annotated[float, Field(ge=0, le=20)] = 10) -> dict:
        """Wait up to 20 seconds for a job. Ending this wait does NOT cancel execution."""
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            result = await bridge.request('GET', f'/executions/{execution_id}')
            remaining = deadline - asyncio.get_running_loop().time()
            if result['status'] in TERMINAL or remaining <= 0:
                return result
            await asyncio.sleep(min(0.2, remaining))

    @server.tool(annotations=READ)
    async def read_events(session_id: Identifier, after: Annotated[int, Field(ge=0)] = 0,
                          limit: Annotated[int, Field(ge=1, le=200)] = 50) -> dict:
        """Read durable stdout, errors and artifact IDs after a sequence cursor. Save returned cursor for next call."""
        return await bridge.request('GET', f'/sessions/{session_id}/events?after={after}&limit={limit}')

    @server.tool(annotations=WRITE)
    async def interrupt(execution_id: Identifier) -> dict:
        """Request interruption, or cancel a queued job. Follow status until stopping is confirmed."""
        return await bridge.request('POST', f'/executions/{execution_id}/interrupt')

    @server.tool(annotations=WRITE)
    async def close_session(session_id: Identifier) -> dict:
        """Stop the kernel and discard its memory. Historical events and artifacts remain."""
        return await bridge.request('DELETE', f'/sessions/{session_id}')

    @server.tool(annotations=WRITE)
    async def restart_session(session_id: Identifier) -> dict:
        """Explicitly discard Python memory and create a fresh kernel. Never use as an automatic retry."""
        return await bridge.request('POST', f'/sessions/{session_id}/restart')

    @server.tool(annotations=READ)
    async def read_checkpoint(session_id: Identifier) -> dict:
        """Read explicit JSON checkpoint; this is not the kernel's Python memory."""
        return {'checkpoint': await bridge.request('GET', f'/sessions/{session_id}/checkpoint')}

    @server.tool(annotations=READ)
    async def read_artifact(artifact_id: Identifier) -> CallToolResult:
        """Return artifact contents (max 8 MiB), with images as native MCP image blocks for visual inspection."""
        return await bridge.artifact(artifact_id)

    return server


def main():
    parser = argparse.ArgumentParser(description='Local stdio MCP adapter for ai-runtime')
    parser.add_argument('--runtime-url', default='http://127.0.0.1:8765')
    parser.add_argument('--token-file', type=Path, required=True)
    args = parser.parse_args()
    try:
        bridge = Bridge(args.runtime_url, args.token_file)
    except ValueError as exc:
        parser.error(str(exc))
    # stdout is reserved exclusively for the MCP protocol.
    create_server(bridge).run(transport='stdio')


if __name__ == '__main__':
    main()
