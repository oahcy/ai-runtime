"""Local stdio MCP adapter. The separate HTTP daemon owns all kernel state."""
import argparse
import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Annotated, Literal
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
        'Prefer execute_and_wait or call_plugin_and_wait for short operations: they return execution-scoped outputs, or a running job ID. Use read_execution_result to continue without resubmitting. execute remains queue-only. Read images with read_artifact. '
        'Use evaluate_js to execute JavaScript directly in an already connected chrome-devtools or usb-browser target, without Python wrappers. '
        'Never automatically replay unknown executions or restart kernels: inspect external effects first. '
        'Restart discards all Python memory; checkpoints only store explicit JSON. '
        'Use list_plugins/get_plugin to discover local plugins; add_plugin registers a trusted local directory. Plugin load/call/unload run as queued kernel jobs. Use normal Python libraries and the preloaded runtime SDK.'
    ))

    @server.tool(annotations=WRITE)
    async def open_output_preview(session_id: Identifier) -> dict:
        """Get a live browser URL for this kernel's display(), images, HTML and text.

        Reuses one viewer per session. Open the returned URL in a browser.
        Updates and clear_output are automatic; HTML scripts are disabled.
        """
        return await bridge.request('POST', f'/sessions/{session_id}/output-preview')

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
    async def read_execution_result(execution_id: Identifier,
                                    wait_seconds: Annotated[float, Field(ge=0, le=20)] = 0,
                                    after: Annotated[int, Field(ge=0)] = 0,
                                    limit: Annotated[int, Field(ge=1, le=200)] = 100) -> dict:
        """Read only this job's outputs and status. Save cursor; follow has_more. Waiting never cancels execution."""
        return await bridge.request('GET', f'/executions/{execution_id}/result?wait_seconds={wait_seconds}&after={after}&limit={limit}')

    async def finish_short_job(job, wait_seconds):
        try:
            return await read_execution_result(job['id'], wait_seconds)
        except ToolError as exc:
            raise ToolError(f"Job {job['id']} was accepted. Read its result; do not resubmit under a new request_id. {exc}") from None

    @server.tool(annotations=WRITE)
    async def execute_and_wait(session_id: Identifier,
                               request_id: Annotated[str, Field(min_length=1, max_length=128)],
                               code: Annotated[str, Field(min_length=1, max_length=262144)],
                               timeout: Annotated[float, Field(ge=.1, le=3600)] = 30,
                               wait_seconds: Annotated[float, Field(ge=0, le=20)] = 5) -> dict:
        """Execute once, briefly wait, return job status and outputs. If done=false, continue read_execution_result. Identical retries reuse request_id."""
        job = await execute(session_id, request_id, code, timeout)
        return await finish_short_job(job, wait_seconds)

    @server.tool(annotations=WRITE)
    async def call_plugin_and_wait(session_id: Identifier,
                                   plugin_id: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')],
                                   request_id: Annotated[str, Field(min_length=1, max_length=128)],
                                   action: str, arguments: dict,
                                   timeout: Annotated[float, Field(ge=.1, le=3600)] = 30,
                                   wait_seconds: Annotated[float, Field(ge=0, le=20)] = 5) -> dict:
        """Call a plugin once and return job status and outputs. Plugin result is JSON stdout. Waiting does not cancel or replay the action."""
        job = await call_plugin(session_id, plugin_id, request_id, action, arguments, timeout)
        return await finish_short_job(job, wait_seconds)

    @server.tool(annotations=WRITE)
    async def evaluate_js(session_id: Identifier,
                          request_id: Annotated[str, Field(min_length=1, max_length=128)],
                          code: Annotated[str, Field(min_length=1, max_length=262144)],
                          plugin_id: Literal['chrome-devtools', 'usb-browser'] = 'chrome-devtools',
                          context_id: int | None = None,
                          await_promise: bool = True,
                          return_by_value: bool = True,
                          timeout: Annotated[float, Field(ge=.1, le=120)] = 10,
                          wait_seconds: Annotated[float, Field(ge=0, le=20)] = 5) -> dict:
        """Execute JS in the plugin's connected target, not in Python or a local Node kernel.

        Connect the plugin in this Runtime session first. context_id selects a CDP execution
        context (e.g. iframe); omit for default context. This does not transpile TypeScript.
        Code may mutate the application. timeout is CDP wait time, not cancellation of JS.
        Never replay an uncertain outcome. Identical retries must reuse request_id and arguments.
        Returns job metadata and javascript when parsed, omitting duplicate stdout. Other outputs
        and unparsed results are retained; full history remains in read_execution_result.
        exceptionDetails means JS failed even
        if the kernel job succeeded. If done=false, continue read_execution_result with execution_id.
        Use return_by_value=false for objectId references; inspect via plugin variables action.
        """
        arguments = dict(expression=code, await_promise=await_promise,
                         return_by_value=return_by_value, timeout=timeout)
        if context_id is not None:
            arguments['context_id'] = context_id
        action = 'evaluate'
        if plugin_id == 'usb-browser':
            arguments = dict(action=action, arguments=arguments)
            action = 'cdp'
        result = await call_plugin_and_wait(session_id, plugin_id, request_id, action,
                                            arguments, timeout + 5, wait_seconds)
        # Compact only this response; durable execution outputs remain unchanged.
        stdout = ''.join(e.get('payload', {}).get('text', '') for e in result.get('outputs', [])
                         if e.get('kind') == 'output.stream' and e.get('payload', {}).get('name') == 'stdout')
        if result.get('done') and not result.get('has_more') and result.get('status') == 'succeeded':
            try:
                value = json.loads(stdout)
            except (ValueError, TypeError):
                pass
            else:
                if isinstance(value, dict) and ('result' in value or 'exceptionDetails' in value):
                    result['javascript'] = value
                    result['javascript_status'] = 'exception' if 'exceptionDetails' in value else 'succeeded'
                    remaining = [e for e in result.get('outputs', [])
                                 if not (e.get('kind') == 'output.stream'
                                         and e.get('payload', {}).get('name') == 'stdout')]
                    result = {k: v for k, v in result.items() if k != 'outputs'}
                    if remaining:
                        result['outputs'] = remaining
        return result

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


    @server.tool(annotations=READ)
    async def list_plugins() -> dict:
        """List registered local Runtime plugins without executing plugin code."""
        return await bridge.request('GET', '/plugins')

    @server.tool(annotations=READ)
    async def get_plugin(plugin_id: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')]) -> dict:
        """Read plugin manifest, action parameter descriptions and registered path."""
        return await bridge.request('GET', f'/plugins/{plugin_id}')

    @server.tool(annotations=WRITE)
    async def add_plugin(path: str) -> dict:
        """Register a trusted local plugin directory containing plugin.json. No download, pip install or execution."""
        return await bridge.request('POST', '/plugins', {'path': path})

    @server.tool(annotations=WRITE)
    async def load_plugin(session_id: Identifier, plugin_id: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')],
                          request_id: Annotated[str, Field(min_length=1, max_length=128)]) -> dict:
        """Queue plugin creation in this kernel. Returns execution ID; wait and read_events for result."""
        return await bridge.request('POST', f'/sessions/{session_id}/plugins/{plugin_id}/load', {'request_id': request_id})

    @server.tool(annotations=WRITE)
    async def call_plugin(session_id: Identifier, plugin_id: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')],
                          request_id: Annotated[str, Field(min_length=1, max_length=128)],
                          action: str, arguments: dict, timeout: Annotated[float, Field(ge=0.1, le=3600)] = 30) -> dict:
        """Queue a manifest action, loading the plugin if needed. Returns execution ID, not action result. Reuse request_id only for identical retries."""
        return await bridge.request('POST', f'/sessions/{session_id}/plugins/{plugin_id}/call',
                                    {'request_id': request_id, 'action': action, 'arguments': arguments, 'timeout': timeout})

    @server.tool(annotations=WRITE)
    async def unload_plugin(session_id: Identifier, plugin_id: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')],
                            request_id: Annotated[str, Field(min_length=1, max_length=128)]) -> dict:
        """Queue plugin close() and unload its kernel instance; registration is retained. Returns execution ID."""
        return await bridge.request('POST', f'/sessions/{session_id}/plugins/{plugin_id}/unload', {'request_id': request_id})

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
