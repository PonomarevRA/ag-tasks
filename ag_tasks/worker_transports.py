"""Host-configured JSONL/HTTP adapters for any agent implementing worker/v1."""

import asyncio
import json
import os
import signal
from uuid import uuid4

import httpx

from .agent_worker_client import HttpWorkerClient
from .issues import DomainError
from .service_values import encode
from .worker_protocol import request, response, capabilities, role_result, validate_run_params


class JsonlAgent:
    """Launch one explicitly configured adapter per role; no vendor command or default exists."""

    def __init__(self, argv):
        """Accept an operator-owned argv list, never a shell command from task data."""
        if type(argv) is not list or not argv or len(argv) > 32:
            raise ValueError('Configure an explicit worker argv list.')
        if any(type(arg) is not str or not arg or '\0' in arg for arg in argv):
            raise ValueError('Worker argv entries must be nonempty strings.')
        self.argv = tuple(argv)

    async def invoke(self, params, *, before_send=None):
        """Negotiate then send once; timeout/EOF kills only our group and remains unknown delivery."""
        validate_run_params(params)
        timeout = params['budget']['timeoutSeconds']
        process = None
        try:
            async with asyncio.timeout(timeout):
                process = await asyncio.create_subprocess_exec(*self.argv,
                    cwd=params['workspace']['path'], stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=os.name != 'nt', limit=16385)
                handshake = request('negotiate', uuid4().hex, {})
                profile = capabilities(await self._exchange(process, handshake), params['role'])
                if before_send:
                    await before_send(profile)
                value = await self._exchange(process, request('run', uuid4().hex, params))
                result = role_result(value, params['role'])
                process.stdin.close()
                await process.wait()
                if process.returncode != 0:
                    raise DomainError('worker_failed', 'Adapter exited unsuccessfully.', 503)
                return {'profile': profile, 'result': result}
        except (TimeoutError, OSError, ValueError, asyncio.IncompleteReadError) as error:
            raise DomainError('worker_delivery_unknown', 'Adapter did not return a bounded terminal response.', 503) from error
        finally:
            if process and process.returncode is None:
                await self._stop(process)

    @staticmethod
    async def _exchange(process, frame):
        """Flush one frame and read only its matching bounded response; no logs are persisted."""
        process.stdin.write(encode(frame, 24576).encode() + b'\n')
        await process.stdin.drain()
        raw = await process.stdout.readline()
        maximum = frame['params']['budget']['maxOutputBytes'] if frame['method'] == 'run' else 16384
        if not raw or len(raw.rstrip(b'\n')) > maximum:
            raise ValueError('Worker response missing or oversized')
        return response(json.loads(raw), frame)

    @staticmethod
    async def _stop(process):
        """Bound cleanup of the process group we spawned, without claiming universal runtime release."""
        try:
            if os.name != 'nt':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


class HttpAgent:
    """Use the same frames with an explicitly selected loopback agent service."""

    def __init__(self, origin, *, client=None):
        """Reuse loopback validation; disable proxies, redirects and transport retries."""
        validator = HttpWorkerClient(origin, 'worker-origin-validation')
        self.origin = validator.origin
        validator.close()
        self.client = client

    async def invoke(self, params, *, before_send=None):
        """Negotiate then POST one role request; never fail over after ambiguous delivery."""
        validate_run_params(params)
        async with asyncio.timeout(params['budget']['timeoutSeconds']):
            if self.client is not None:
                return await self._invoke(self.client, params, before_send)
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                         timeout=params['budget']['timeoutSeconds']) as client:
                return await self._invoke(client, params, before_send)

    async def _invoke(self, client, params, before_send):
        """Keep protocol checks identical to JSONL, including role/capability admission."""
        profile = capabilities(await self._exchange(client, request('negotiate', uuid4().hex, {})), params['role'])
        if before_send:
            await before_send(profile)
        value = await self._exchange(client, request('run', uuid4().hex, params))
        return {'profile': profile, 'result': role_result(value, params['role'])}

    async def _exchange(self, client, frame):
        """Stream into a fixed response budget rather than trust an arbitrary Content-Length."""
        maximum = frame['params']['budget']['maxOutputBytes'] if frame['method'] == 'run' else 16384
        async with client.stream('POST', self.origin + '/worker',
                                 content=encode(frame, 24576).encode(),
                                 headers={'Content-Type': 'application/json'}) as reply:
            reply.raise_for_status()
            raw = bytearray()
            async for chunk in reply.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > maximum:
                    raise DomainError('worker_response_too_large', 'Adapter response exceeds16KiB.', 413)
        return response(json.loads(raw), frame)
