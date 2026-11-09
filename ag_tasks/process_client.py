"""Fixed Tasks HTTP calls used by an external, agent-neutral workflow driver."""

from urllib.parse import quote
from uuid import uuid4

from .agent_worker_client import HttpWorkerClient
from .issues import DomainError


class ProcessClient(HttpWorkerClient):
    """Reuse the owner API and its gates; never write the service database directly."""

    def read(self, path):
        """Read a driver-selected fixed path, propagating domain errors without retry."""
        return self._reply(self.client.get(self.origin + path))

    def post(self, path, payload):
        """Submit an existing workflow operation; ambiguous errors require current-state reconciliation."""
        return self._reply(self.client.post(self.origin + '/api/v1/agent/' + path, json=payload))

    def command(self, operation, payload, key):
        """Retain an exact caller intent for durable command-outcome recovery."""
        return self._reply(self.client.post(self.origin + '/api/v1/agent/commands', json={
            'clientId': self.client_id, 'commandId': key, 'operation': operation, 'payload': payload}))

    def issue(self, identifier):
        """Read one canonical task including current result/review state."""
        return self.read('/api/v1/issues/' + quote(identifier, safe=''))

    @staticmethod
    def _reply(response):
        """Preserve a safe domain code without echoing task text or credentials."""
        value = response.json()
        if response.is_error or 'error' in value:
            error = value.get('error', {})
            raise DomainError(error.get('code', 'tasks_unavailable'), error.get('message', 'Tasks request failed.'), response.status_code)
        return value
