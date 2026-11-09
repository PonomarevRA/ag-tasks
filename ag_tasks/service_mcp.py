"""MCP is an adapter to universal domain commands and reads, not a second service core."""

from urllib.parse import quote


def register(mcp, request):
    """Advertise fixed domain commands and reads; no arbitrary HTTP, process launch or approval tool."""

    from .process_mcp import register as register_process
    register_process(mcp, request)

    @mcp.tool()
    def tasks_service_capabilities() -> dict:
        """Read common command/event limits, queue coordination and explicit runtime capability boundaries."""
        return request('GET', '/api/v1/service-data')

    @mcp.tool()
    def tasks_command(client_id: str, command_id: str, operation: str, payload: dict) -> dict:
        """Execute an advertised domain command; preserve the key on retry and reject different payloads."""
        return request('POST', '/api/v1/agent/commands',
                       {'clientId': client_id, 'commandId': command_id, 'operation': operation, 'payload': payload})

    @mcp.tool()
    def tasks_ensure_project_space(project_key: str, name: str, client_id: str, command_id: str,
                                   prefix: str | None = None) -> dict:
        """
        Find/create one project space through a durable idempotent command.

        Use an explicit stable project_key and human name. Retain client_id and
        command_id on retry; returned id is the space_id for root plans/tasks.
        Existing ownership/name/prefix never change on repeat calls.
        """
        payload = {'projectKey':project_key,'name':name}
        if prefix is not None:
            payload['prefix'] = prefix
        return request('POST', '/api/v1/agent/commands', {'clientId':client_id,'commandId':command_id,
                       'operation':'project.ensure','payload':payload})

    @mcp.tool()
    def tasks_project_tree(root_id: str) -> dict:
        """Read exact root IDs and current space for migration, including archived/deleted descendants and critics."""
        return request('GET', '/api/v1/project-trees/' + quote(root_id, safe=''))

    @mcp.tool()
    def tasks_move_project_tree(root_id: str, project_key: str, expected_space_id: str,
                                expected_ids: list[str], client_id: str, command_id: str) -> dict:
        """
        Atomically migrate a user-requested exact root tree while preserving old keys and evidence.

        Read all descendants first (including critics); retain command identity
        on retry. Membership/ownership conflicts require a fresh tree read.
        This operation does not approve, restart, complete or redefine any task.
        """
        return request('POST', '/api/v1/agent/commands', {'clientId':client_id,'commandId':command_id,
                       'operation':'project.move_tree','payload':{'rootId':root_id,'projectKey':project_key,
                       'expectedSpaceId':expected_space_id,'expectedIds':expected_ids}})

    @mcp.tool()
    def tasks_command_outcome(client_id: str, command_id: str) -> dict:
        """Read an earlier committed outcome after a lost ACK; missing is not permission to replay unknown work."""
        return request('GET', '/api/v1/commands/' + quote(client_id, safe='') + '/' + quote(command_id, safe=''))

    @mcp.tool()
    def tasks_events(limit: int = 20, after: int = 0) -> dict:
        """Read universal command facts with a durable cursor; independent of ACP or the submitting adapter."""
        return request('GET', '/api/v1/events', params={'limit': limit, 'after': after})

    @mcp.tool()
    def tasks_sessions(limit: int = 20, after: str | int = 0, issue_id: str | None = None,
                       sort: str = 'newest', active_only: bool = False) -> dict:
        """Read sessions newest/oldest by activity; pass nextCursor unchanged with the same filters."""
        return request('GET', '/api/v1/sessions', params={'limit': limit, 'after': after, 'issueId': issue_id,
                                                       'sort': sort, 'activeOnly': active_only})

    @mcp.tool()
    def tasks_session(session_id: str) -> dict:
        """Read one session and its bounded projections, without controlling the agent."""
        return request('GET', '/api/v1/sessions/' + quote(session_id, safe=''))

    @mcp.tool()
    def tasks_session_events(session_id: str, limit: int = 20, after: int = 0) -> dict:
        """Read durable original observations, including unknown extensions and redaction markers."""
        return request('GET', '/api/v1/sessions/' + quote(session_id, safe='') + '/events',
                       params={'limit': limit, 'after': after})

    @mcp.tool()
    def tasks_queues(limit: int = 20, after: int = 0) -> dict:
        """Read common agent lanes; mutations use tasks_command with durable caller keys."""
        return request('GET', '/api/v1/queues', params={'limit': limit, 'after': after})

    @mcp.tool()
    def tasks_queue(lane_id: str) -> dict:
        """Read lane profile, revision, drain state and current reserved attempt."""
        return request('GET', '/api/v1/queues/' + quote(lane_id, safe=''))

    @mcp.tool()
    def tasks_queue_items(lane_id: str, limit: int = 20, after: int = 0, pending: bool = True) -> dict:
        """Read waiting dispatch order or creation history; reorder invalidates pending pagination."""
        suffix = '/pending' if pending else '/items'
        return request('GET', '/api/v1/queues/' + quote(lane_id, safe='') + suffix,
                       params={'limit': limit, 'after': after})

    @mcp.tool()
    def tasks_attempt(attempt_id: str) -> dict:
        """Read one fenced runtime attempt without renewing its lease or inferring process shutdown."""
        return request('GET', '/api/v1/attempts/' + quote(attempt_id, safe=''))
