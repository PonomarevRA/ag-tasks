"""Bounded standalone MCP tools for issues; approval and arbitrary commands are absent."""

import ipaddress
import json
import os
from urllib.parse import quote, urlsplit
from typing import Literal

import httpx


def local_origin(value):
    """Accept explicit numeric loopback HTTP origins without credentials, paths or redirects."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("Invalid local origin")
    if not ipaddress.ip_address(parsed.hostname or "").is_loopback or not parsed.port:
        raise ValueError("Invalid local origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Invalid local origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def build_mcp(origin=None, directory=None, client=None, *, desktop_automation=False):
    """Connect only to ag-tasks HTTP; never open a second database or use Observer internals."""
    from .mcp_presence import ObservedMCP
    from .app import default_directory
    configured = origin or os.environ.get("AG_TASKS_URL")
    if configured:
        local_origin(configured)
    directory = directory or default_directory()
    transport = client or httpx.Client(timeout=30, trust_env=False, follow_redirects=False)
    from .planning_mcp import WORKFLOW
    from .desktop_automation import ROUTE, TOKEN_HEADER, desktop_enabled
    desktop_automation = desktop_automation or desktop_enabled(directory)

    def bounded_response(method, url, **kwargs):
        """Limit bridge responses even when a local endpoint misbehaves."""
        with transport.stream(method, url, **kwargs) as response:
            raw = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                if len(raw) + len(chunk) > 2_000_000:
                    raise ValueError("Response limit")
                raw.extend(chunk)
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("Invalid response object")
            if response.status_code >= 400:
                return {"error": result.get("error", {"code": "request_failed"})}
            return result

    def request(method, path, payload=None, params=None, *, timeout=30):
        """Resolve current standalone identity each time; expose only fixed tool routes below."""
        try:
            expected = None
            base = configured
            desktop_route = path.startswith(ROUTE + "/")
            location = {}
            if desktop_route and (not desktop_automation or configured is not None):
                return {"error": {"code": "desktop_discovery_required"}}
            if base is None:
                endpoint = directory / "endpoint.json"
                if endpoint.exists():
                    if endpoint.stat().st_size > 1024:
                        raise ValueError("Invalid discovery")
                    location = json.loads(endpoint.read_text(encoding="utf-8"))
                    base, expected = location["origin"], location["instanceId"]
                else:
                    # Missing discovery can be a brief owner restart. Never
                    # attach an explicitly selected data directory to some
                    # other local store listening on the historical port.
                    return {"error": {"code": "tasks_unavailable"}}
            base = local_origin(base)
            status = bounded_response("GET", base + "/api/v1/status", timeout=timeout)
            if status.get("application") != "ag-tasks" or (expected and status.get("instanceId") != expected):
                raise ValueError("Identity mismatch")
            arguments = {"params": {key: value for key, value in (params or {}).items() if value is not None}}
            if payload is not None:
                encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
                if len(encoded) > 65536:
                    raise ValueError("Request limit")
                arguments["content"] = encoded
                arguments["headers"] = {"Content-Type": "application/json"}
            if desktop_route:
                token = location.get("desktopToken")
                if not isinstance(token, str) or len(token) != 64:
                    return {"error": {"code": "desktop_disabled"}}
                arguments.setdefault("headers", {})[TOKEN_HEADER] = token
            return bounded_response(method, base + path, timeout=timeout, **arguments)
        except (httpx.HTTPError, ValueError, KeyError, OSError, TypeError):
            return {"error": {"code": "tasks_unavailable"}}

    def issue_path(identifier):
        """Encode an opaque issue identity into one path segment."""
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 64 or "/" in identifier or identifier in {".", ".."}:
            raise ValueError("Invalid issue identity")
        return quote(identifier, safe="")

    mcp = ObservedMCP("ag-tasks", request=request, instructions=WORKFLOW)

    @mcp.tool()
    def tasks_capabilities() -> dict:
        """Read live owner version and execution contract; cached IDE tool lists are not capability evidence."""
        return request("GET", "/api/v1/status")

    @mcp.tool()
    def tasks_work_history(issue_id: str, limit: int = 20, offset: int = 0) -> dict:
        """Read paginated work duration and captured roles, without exposing session keys or granting work authority."""
        return request("GET", "/api/v1/issues/" + issue_path(issue_id) + "/work/history", params={"limit":limit,"offset":offset})

    @mcp.tool()
    def tasks_list(status: str | None = None, parent_id: str | None = None, repo_id: str | None = None,
                   limit: int = 50, offset: int = 0, space_id: str | None = None,
                   include_archived: bool = False) -> dict:
        """List issues, kanban status, parent links and evidence references."""
        return request("GET", "/api/v1/issues", params={"status": status, "parentId": parent_id, "repoId": repo_id,
                        "limit": max(1, min(limit, 200)), "offset": max(0, offset), "spaceId": space_id,
                        "includeArchived": include_archived})

    @mcp.tool()
    def tasks_get(issue_id: str) -> dict:
        """Read issue detail, comments and attributed audit history."""
        return request("GET", "/api/v1/issues/" + issue_path(issue_id))

    @mcp.tool()
    def tasks_repositories() -> dict:
        """Read repository IDs explicitly registered by the user; cannot register arbitrary paths."""
        return request("GET", "/api/v1/repositories")

    @mcp.tool()
    def tasks_create(title: str, description: str = "", priority: str = "normal", parent_id: str | None = None,
                     repo_id: str | None = None, assignee: str | None = None,
                     external_refs: list[dict] | None = None, space_id: str | None = None,
                     intended_role: str | None = None, dor: list[dict] | None = None, dod: list[dict] | None = None, kind: str = "task",
                     estimated_complexity: dict | None = None, code_task: bool = False,
                     languages: list[str] | None = None, systems: list[str] | None = None,
                     project_key: str | None = None, project_name: str | None = None, project_prefix: str | None = None,
                     due_date: str | None = None) -> dict:
        """Create an issue; optional due_date is an inclusive UTC date YYYY-MM-DD, not a completion gate."""
        payload = {"title": title, "description": description, "priority": priority, "parentId": parent_id,
                   "repoId": repo_id, "assignee": assignee, "externalRefs": external_refs or [],
                   "intendedRole": intended_role, "dor": dor or [], "dod": dod or []}
        payload.update(codeTask=code_task, languages=languages or [], systems=systems or [])
        if due_date is not None:
            payload['dueDate'] = due_date
        for name, value in [('projectKey',project_key),('projectName',project_name),('projectPrefix',project_prefix)]:
            if value is not None:
                payload[name] = value
        payload["kind"] = kind
        if estimated_complexity is not None:
            payload["estimatedComplexity"] = estimated_complexity
        if space_id is not None:
            payload["spaceId"] = space_id
        return request("POST", "/api/v1/agent/issues", payload)

    @mcp.tool()
    def tasks_update(issue_id: str, status: str | None = None, title: str | None = None,
                     description: str | None = None, priority: str | None = None, assignee: str | None = None,
                     space_id: str | None = None, intended_role: str | None = None,
                     dor: list[dict] | None = None, dod: list[dict] | None = None,
                     estimated_complexity: dict | None = None, code_task: bool | None = None,
                     languages: list[str] | None = None, systems: list[str] | None = None,
                     due_date: str | None = None, clear_due_date: bool = False) -> dict:
        """Edit metadata/criteria under existing gates. due_date sets YYYY-MM-DD UTC; clear_due_date clears it, mutually exclusively. Omission preserves the deadline; scheduling alone never starts/stops work or invalidates acceptance."""
        patch = {"status": status, "title": title, "description": description, "priority": priority, "assignee": assignee,
                 "spaceId": space_id, "intendedRole": intended_role, "dor": dor, "dod": dod, "estimatedComplexity": estimated_complexity, "codeTask": code_task, "languages": languages, "systems": systems}
        patch = {key: value for key, value in patch.items() if value is not None}
        if due_date is not None and clear_due_date:
            return {'error': {'code': 'invalid_due_date', 'message': 'Set due_date or clear_due_date, not both.'}}
        if due_date is not None or clear_due_date:
            patch['dueDate'] = None if clear_due_date else due_date
        return request("PATCH", "/api/v1/agent/issues/" + issue_path(issue_id), patch)

    @mcp.tool()
    def tasks_link(issue_id: str, related_id: str, kind: str = "related") -> dict:
        """Link another issue or declare a blocker; subtasks are created with parent_id."""
        return request("POST", "/api/v1/agent/issues/" + issue_path(issue_id) + "/links", {"relatedId": related_id, "kind": kind})

    @mcp.tool()
    def tasks_comment(issue_id: str, text: str) -> dict:
        """Append findings and agent-reported checks; text is evidence, never an executable command."""
        return request("POST", "/api/v1/agent/issues/" + issue_path(issue_id) + "/comments", {"text": text})

    @mcp.tool()
    def tasks_prepare_branch(issue_id: str) -> dict:
        """Prepare an isolated codex worktree from a registered committed base; this does not start work or its timer."""
        return request("POST", "/api/v1/agent/issues/" + issue_path(issue_id) + "/prepare", {})

    @mcp.tool()
    def tasks_review(issue_id: str) -> dict:
        """Read exact base/head revisions and bounded diff; this tool cannot approve or merge."""
        return request("GET", "/api/v1/issues/" + issue_path(issue_id) + "/review")

    @mcp.tool()
    def tasks_submit_review(issue_id: str) -> dict:
        """Compatibility: refresh Git evidence and request guarded review; a current submitted result and passed DoD remain required."""
        path = issue_path(issue_id)
        review = request("GET", "/api/v1/issues/" + path + "/review")
        if "error" in review:
            return review
        return request("PATCH", "/api/v1/agent/issues/" + path, {"status": "in_review"})

    @mcp.tool()
    def tasks_spaces() -> dict:
        """Read spaces and their project keys; use tasks_ensure_project_space to find/create a project owner."""
        return request("GET", "/api/v1/spaces")

    @mcp.tool()
    def tasks_graph(space_id: str = "default", limit: int = 200) -> dict:
        """Read a bounded same-space graph with explicit truncation, totals and boundary stubs."""
        return request("GET", "/api/v1/issues/graph", params={"spaceId": space_id, "limit": limit})

    @mcp.tool()
    def tasks_decompose(issue_id: str, children: list[dict]) -> dict:
        """Atomically create up to fifty ordinary child issues; parent/space inheritance and field validation are server-owned."""
        return request("POST", "/api/v1/agent/issues/" + issue_path(issue_id) + "/decompose", {"children": children})

    @mcp.tool()
    def tasks_work_start(issue_id: str, agent_id: str, session_key: str) -> dict:
        """Start or replay a server-owned 90 second work lease after role/DoR/blocker checks; identities are declared, not authenticated."""
        return request("POST", "/api/v1/agent/issues/" + issue_path(issue_id) + "/work/start",
                       {"agentId": agent_id, "sessionKey": session_key})

    @mcp.tool()
    def tasks_work_heartbeat(issue_id: str, agent_id: str, session_id: str, session_key: str) -> dict:
        """Renew the exact active session from a runner about every 30 seconds; UI refresh/model wakeups must not drive this lease.

        The server rejects stale, expired or mismatched identities and owns all elapsed timing.
        """
        return request("POST", "/api/v1/agent/issues/" + issue_path(issue_id) + "/work/heartbeat",
                       {"agentId": agent_id, "sessionId": session_id, "sessionKey": session_key})

    @mcp.tool()
    def tasks_work_stop(issue_id: str, agent_id: str, session_id: str, session_key: str) -> dict:
        """Idempotently pause the exact work session; stopping does not submit a result, approve review or invent elapsed time."""
        return request("POST", "/api/v1/agent/issues/" + issue_path(issue_id) + "/work/stop",
                       {"agentId": agent_id, "sessionId": session_id, "sessionKey": session_key})

    @mcp.tool()
    def tasks_submit_result(issue_id: str, agent_id: str, session_id: str, session_key: str,
                            summary: str, changes: list[str], validation: list[str], limitations: str = "",
                            artifacts: list[dict] | None = None) -> dict:
        """Submit results from a live session; success closes work and creates a critic after DoD checks.

        Artifacts accept at most 20 objects with HTTP(S) url and optional title/kind. Put local paths
        in validation text or task Markdown. On error correct the payload; record actual complexity
        only after a successful response contains currentResult. Do not retry a lost session blindly.
        """
        from .criteria import result as validate_result
        from .issues import DomainError
        result = {"summary": summary, "changes": changes, "validation": validation,
                  "limitations": limitations, "artifacts": [] if artifacts is None else artifacts}
        try:
            result = validate_result(result)
        except DomainError as error:
            failure = {"code": error.code, "message": error.message}
            if error.details is not None:
                failure["details"] = error.details
            return {"error": failure}
        return request("POST", "/api/v1/agent/issues/" + issue_path(issue_id) + "/results",
                       {"agentId": agent_id, "sessionId": session_id, "sessionKey": session_key, "result": result})

    @mcp.tool()
    def tasks_review_report(critic_id: str, agent_id: str, session_id: str, session_key: str,
                            generation: int, definition_revision: int, verdict: Literal["pass", "rework"],
                            summary: str, findings: list[str], evidence: str, reviewed_head: str | None = None) -> dict:
        """Report declared critic evidence for the exact parent generation/definition/head; no report grants human merge authority."""
        return request("POST", "/api/v1/agent/issues/" + issue_path(critic_id) + "/review-report",
                       {"agentId": agent_id, "sessionId": session_id, "sessionKey": session_key,
                        "generation": generation, "definitionRevision": definition_revision, "reviewedHead": reviewed_head,
                        "verdict": verdict, "summary": summary, "findings": findings, "evidence": evidence})

    from .planning_mcp import register
    register(mcp, request, issue_path)
    from .service_mcp import register as register_service_data
    register_service_data(mcp, request)
    from .companion_mcp import register as register_companion
    register_companion(mcp, request)
    if desktop_automation:
        from .desktop_automation_mcp import register as register_desktop
        register_desktop(mcp, request)
    return mcp
