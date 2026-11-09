"""Optional guidance is data; real task completion must not depend on a memory provider."""

import asyncio
from hashlib import sha256
from pathlib import Path

import pytest
import json

from fastapi.testclient import TestClient
import httpx

from ag_tasks.app import create_app
from ag_tasks.issues import IssueStore
from ag_tasks.mcp_server import build_mcp
from ag_tasks.task_context import serialized
from workflow_helpers import metadata, review_ready
from test_mcp_workflow import call
from ag_tasks.process_worker import ArtifactCheck, ProcessWorker


def test_result_and_review_recommendation_is_ephemeral_and_never_a_completion_gate(tmp_path):
    """Current review completes without a memory tool or any memory state in persisted results."""
    store = IssueStore(tmp_path)
    try:
        issue = store.create({'title': 'No memory integration', **metadata()})
        original_submit = store.results.submit
        submitted = []
        def capture(*args, **kwargs):
            """Observe the public producer response without bypassing its actual validation."""
            value = original_submit(*args, **kwargs)
            submitted.append(value)
            return value
        store.results.submit = capture
        reviewed = review_ready(store, issue['id'])
        assert reviewed['status'] == 'done' and reviewed['currentResultIsCurrent']
        advice = reviewed['memoryRecommendation']
        assert advice['selectionOwner'] == 'agent' and advice['toolSource'] == 'current_agent_session'
        assert advice['required'] is False and advice['blocksCompletion'] is False
        assert advice['whenUnavailable'] == 'skip' and advice['fallback'] is None
        assert submitted[0]['memoryRecommendation'] == advice
        # Advice is service-owned metadata, never a mutable field in the immutable user result.
        payload = json.loads(store.db.connection.execute('SELECT payload FROM results WHERE issue_id=?', (issue['id'],)).fetchone()[0])
        assert 'memoryRecommendation' not in payload
        assert 'memoryRecommendation' not in store.get(issue['id'])['currentResult']
        advice['instructions'] = 'caller mutation'
        assert store.service.task_context.get(issue['id'])['memoryRecommendation']['instructions'] != 'caller mutation'
    finally:
        store.db.close()


def test_http_mcp_and_workflow_supply_guidance_without_tool_discovery_or_storage(tmp_path):
    """Only existing read routes are used; bounded full context retains the advisory block."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as http:
        store = http.app.state.store
        issue = store.create({'title': 'Provider-neutral continuation', 'description': 'context ' * 2000, **metadata()})
        requests = []
        def handle(request):
            """Bridge only this private owner and record all requests for a no-mutation assertion."""
            requests.append((request.method, request.url.path))
            reply = http.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            return httpx.Response(reply.status_code, content=reply.content, headers=reply.headers)
        before = store.db.connection.total_changes
        with httpx.Client(transport=httpx.MockTransport(handle)) as bridge:
            mcp = build_mcp(origin='http://127.0.0.1:8895', directory=tmp_path, client=bridge)
            workflow = call(mcp, 'tasks_planning_rules')
            packet = call(mcp, 'tasks_task_context', issue_id=issue['id'], max_bytes=8192)
            assert 'error' not in workflow and 'error' not in packet
        direct = http.get('/api/v1/issues/' + issue['id'] + '/context?maxBytes=8192').json()
        assert packet['memoryRecommendation'] == direct['memoryRecommendation'] == workflow['memoryRecommendation']
        assert len(serialized(packet)) <= 8192
        assert packet['memoryRecommendation']['instructions'] in workflow['instructions']
        assert 'memoryRecommendation' not in packet['omitted']
        assert store.db.connection.total_changes == before
        assert all(method == 'GET' for method, _ in requests)
        caps = store.service.capabilities()['process']
        assert caps['artifactExport'] is False and caps['memoryAndLearning'] is False
        assert caps['memoryRecommendation'] == packet['memoryRecommendation']['contract']
        assert not (tmp_path / 'artifacts' / 'ag-tasks').exists()


def test_interrupted_worker_does_not_create_fallback_or_touch_old_exports(tmp_path):
    """Failure propagates to the caller without a finally-block export or an invented save status."""
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    source = workspace / 'input.txt'
    source.write_text('Read-only source')
    previous = workspace / 'artifacts' / 'ag-tasks' / 'previous.json'
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b'user-owned-old-export')
    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    with TestClient(create_app(tmp_path / 'store'), base_url='http://127.0.0.1:8895') as http:
        from ag_tasks.process_client import ProcessClient
        def handle(request):
            """Use only this test's real owner routes, never a memory service."""
            reply = http.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            return httpx.Response(reply.status_code, content=reply.content, headers=reply.headers)
        bridge = httpx.Client(transport=httpx.MockTransport(handle))
        client = ProcessClient('http://127.0.0.1:8895', 'interrupted', client=bridge)
        criteria = [{'id': 'artifact', 'text': 'Read input', 'passed': False, 'evidence': ''}]
        issue = client.command('issue.create', {'title': 'Interrupted task', 'intendedRole': 'developer',
            'dor': criteria, 'dod': criteria}, 'create')
        class InterruptedAgent:
            """Advertise no memory tools; fail before any result can be committed."""
            async def invoke(self, params, before_send):
                """Observe advisory input then simulate an external adapter interruption."""
                assert params['input']['memoryRecommendation']['whenUnavailable'] == 'skip'
                raise RuntimeError('adapter interrupted')
        try:
            worker = ProcessWorker(client, InterruptedAgent(), worker_id='interrupted')
            with pytest.raises(RuntimeError, match='adapter interrupted'):
                asyncio.run(worker.task_cycle(issue['id'], ArtifactCheck(workspace, 'input.txt', sha256(source.read_bytes()).hexdigest()), run_key='failure'))
            assert {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()} == before
            assert client.issue(issue['id'])['currentResult'] is None
        finally:
            client.close()
