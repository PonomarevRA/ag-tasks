"""Optional MCP adapter to vendor-neutral process evidence and run reads."""

from urllib.parse import quote


def register(mcp, request):
    """Use fixed owner routes; existing tasks_command carries advertised process mutations."""
    @mcp.tool()
    def tasks_process_evidence(issue_id: str, limit: int = 20) -> dict:
        """List retained evidence references for one task without starting external work."""
        return request('GET', '/api/v1/process/evidence', params={'issueId': issue_id, 'limit': limit})

    @mcp.tool()
    def tasks_process_manifest(evidence_id: str) -> dict:
        """Read a durable manifest; declared observations are not verified causes."""
        return request('GET', '/api/v1/process/evidence/' + quote(evidence_id, safe=''))

    @mcp.tool()
    def tasks_process_run(run_id: str) -> dict:
        """Read exact delivery state after a lost acknowledgement without retrying it."""
        return request('GET', '/api/v1/process/runs/' + quote(run_id, safe=''))
