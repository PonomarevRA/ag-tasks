"""Bounded NDJSON transport for one explicitly launched ACP process."""

import asyncio
import json
from pathlib import Path

from .acp_projection import validate
from .issues import DomainError
from .service_values import encode

MAX_FRAME_BYTES = 15360


class AcpTransportError(RuntimeError):
    """A transport fault leaves prompt delivery and external effects uncertain."""


class AcpProcess:
    """Own only the direct subprocess; its exit never proves detached children stopped."""

    def __init__(self, process):
        """Retain a single bounded reader task so polling cannot discard partial frames."""
        self.process = process
        self.pending_read = None

    @classmethod
    async def start(cls, argv, cwd):
        """Launch operator-supplied argv without a shell or queued-text interpolation."""
        if not argv or any(type(value) is not str or not value for value in argv):
            raise ValueError('ACP argv must be a nonempty list of strings.')
        directory = Path(cwd)
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError('ACP cwd must be an existing absolute directory.')
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(directory), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_FRAME_BYTES + 1)
        return cls(process)

    async def send(self, message):
        """Write one finite JSON object; a failed drain is an ambiguous send, never retried."""
        encoded = encode(message, MAX_FRAME_BYTES).encode('utf-8') + b'\n'
        try:
            self.process.stdin.write(encoded)
            await asyncio.wait_for(self.process.stdin.drain(), 5)
        except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError) as error:
            raise AcpTransportError('ACP send failed; delivery is unknown.') from error

    async def poll(self, seconds=0.2):
        """Return one object or None while waiting; reject malformed, huge or unterminated frames."""
        if self.pending_read is None:
            self.pending_read = asyncio.create_task(self.process.stdout.readline())
        done, _ = await asyncio.wait({self.pending_read}, timeout=seconds)
        if not done:
            return None
        task, self.pending_read = self.pending_read, None
        try:
            data = task.result()
            if not data:
                raise AcpTransportError('ACP EOF before a foreground response.')
            if len(data) > MAX_FRAME_BYTES or not data.endswith(b'\n'):
                raise AcpTransportError('ACP frame exceeds its byte budget or lacks a newline.')
            message = json.loads(data.decode('utf-8'))
            if type(message) is not dict or message.get('jsonrpc') != '2.0':
                raise AcpTransportError('ACP requires JSON-RPC 2.0 object frames.')
            validate(message)
            encode(message, MAX_FRAME_BYTES)
            return message
        except (ValueError, UnicodeError, DomainError) as error:
            raise AcpTransportError('Malformed ACP frame.') from error

    async def close(self):
        """Bound direct-process cleanup without claiming ownership of detached descendants."""
        if self.pending_read is not None:
            self.pending_read.cancel()
            await asyncio.gather(self.pending_read, return_exceptions=True)
            self.pending_read = None
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
                await self.process.wait()
        if self.process.stdin:
            self.process.stdin.close()
