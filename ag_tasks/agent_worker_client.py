"""Fixed loopback service adapter for an explicitly invoked external worker."""

import ipaddress
from urllib.parse import quote, urlsplit

import httpx


class HttpWorkerClient:
    """Use the same service commands as MCP/UI, with no transport-level automatic retries."""

    def __init__(self, origin, client_id, *, client=None):
        """Accept a loopback HTTP service only; redirects and ambient proxy credentials are disabled."""
        parsed = urlsplit(origin)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            loopback = parsed.hostname == 'localhost'
        has_credentials = parsed.username is not None or parsed.password is not None
        has_extra_location = parsed.path not in ('', '/') or bool(parsed.query) or bool(parsed.fragment)
        if parsed.scheme != 'http' or not loopback or has_credentials or has_extra_location:
            raise ValueError('Worker service origin must be plain loopback HTTP without credentials or a path.')
        self.origin, self.client_id = origin.rstrip('/'), client_id
        self.client = client or httpx.Client(timeout=5, follow_redirects=False, trust_env=False)

    def execute(self, operation, payload, command_id):
        """Submit one explicit intent identity; an ambiguous network failure is returned to the worker."""
        response = self.client.post(self.origin + '/api/v1/agent/commands', json={
            'clientId': self.client_id, 'commandId': command_id,
            'operation': operation, 'payload': payload})
        response.raise_for_status()
        return response.json()

    def attempt(self, attempt_id):
        """Read current ownership, rather than trusting a cached idempotent dispatch response."""
        response = self.client.get(self.origin + '/api/v1/attempts/' + quote(attempt_id, safe=''))
        response.raise_for_status()
        return response.json()

    def close(self):
        """Release only this worker's HTTP connection pool."""
        self.client.close()
