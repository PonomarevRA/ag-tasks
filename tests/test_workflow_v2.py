"""Space/lifecycle acceptance and durable result evidence, independent of frontend rendering."""

from datetime import datetime, timezone
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ag_tasks.app import create_app
from ag_tasks.database import SCHEMA
from ag_tasks.execution import Execution
from ag_tasks.issues import DomainError, IssueStore
from workflow_helpers import finish_noncode, metadata, review_ready


@pytest.fixture
def store(tmp_path):
    """Never open installed data, a user's repository or an MCP model in domain acceptance tests."""
    value = IssueStore(tmp_path)
    yield value
    value.close()


def session(store, issue_id, key='work'):
    """Start an explicit producer session and return its server-selected ID."""
    active = store.execution.start(issue_id, {'agentId': 'producer', 'sessionKey': key})
    return {'agentId': 'producer', 'sessionKey': key, 'sessionId': active['work']['activeSession']['sessionId']}


def content():
    """Fixture result carries bounded caller-reported checks rather than fabricated provider execution."""
    return {'summary': 'Actual fixture result', 'changes': ['Changed fixture'], 'validation': ['Fixture checks'], 'limitations': '', 'artifacts': []}


def test_decomposition_atomic_same_space_and_real_graph_stubs(store):
    """Failed child creation rolls back the complete plan; bounded graph edges preserve direction and off-page IDs."""
    space = store.spaces.write({'name': 'Project A'})
    parent = store.create({'title': 'Plan', 'spaceId': space['id']})
    before = parent['historyCount']
    with pytest.raises(DomainError):
        store.spaces.decompose(parent['id'], {'children': [{'title': 'Valid'}, {'title': ''}]})
    assert len(store.list()) == 1 and store.get(parent['id'])['historyCount'] == before
    children = store.spaces.decompose(parent['id'], {'children': [{'title': 'First'}, {'title': 'Second'}]})['items']
    assert all(child['spaceId'] == space['id'] and child['work']['elapsedMs'] == 0 for child in children)
    store.link(children[0]['id'], children[1]['id'], 'blocks')
    graph = store.spaces.graph(space['id'], limit=1)
    assert graph['nodeCount'] == 3 and graph['edgeCount'] == 3 and graph['truncated']
    assert graph['nodes'][0]['id'] == children[1]['id']
    assert {'from': children[0]['id'], 'to': children[1]['id'], 'kind': 'blocks'} in graph['edges']
    assert parent['id'] in {item['id'] for item in graph['stubs']}


def test_cross_space_links_are_allowed_but_split_hierarchy_and_cycles_are_rejected(store):
    """Cross-space relations are explicit; hierarchy stays local and scheduling remains acyclic."""
    parent = store.create({'title': 'Parent'})
    child = store.create({'title': 'Child', 'parentId': parent['id']})
    other_space = store.spaces.write({'name': 'Another'})
    other = store.create({'title': 'Other', 'spaceId': other_space['id']})
    assert store.link(child['id'], other['id'])['links'][0]['issueId'] == other['id']
    for operation in (lambda: store.update(parent['id'], {'spaceId': other_space['id']}),
                      lambda: store.update(child['id'], {'parentId': other['id']}),
                      lambda: store.update(parent['id'], {'parentId': child['id']})):
        with pytest.raises(DomainError):
            operation()
    assert store.get(child['id'])['spaceId'] == 'default'


def test_failed_completion_definition_closes_work_and_persists_rework(store):
    """A failed DoD is a durable work outcome, not a transaction rolled back by the public409."""
    values = metadata()
    values['dod'][0].update(passed=False, evidence='')
    issue = store.create({'title': 'Incomplete', **values})
    keys = session(store, issue['id'])
    with pytest.raises(DomainError) as failure:
        store.results.submit(issue['id'], {**keys, 'result': content()})
    assert failure.value.details['status'] == 'rework'
    changed = store.get(issue['id'])
    assert changed['status'] == 'rework' and changed['work']['activeSession'] is None
    assert changed['resultsCount'] == 0
    assert changed['history'][-1]['action'] == 'completion_definition_failed'


def test_result_generations_preserved_and_older_critic_is_superseded(store):
    """Rework creates a new immutable result and closes stale critic work without forging a fresh pass."""
    issue = store.create({'title': 'Rework', **metadata()})
    keys = session(store, issue['id'], 'first')
    first = store.results.submit(issue['id'], {**keys, 'result': content()})
    critic_id = first['criticReviewId']
    store.execution.start(critic_id, {'agentId': 'critic', 'sessionKey': 'old-review'})
    keys = session(store, issue['id'], 'second')
    second = store.results.submit(issue['id'], {**keys, 'result': {**content(), 'summary': 'Second immutable result'}})
    assert second['resultsCount'] == 2 and second['currentResult']['generation'] == 2
    assert second['results'][1]['summary'] == 'Actual fixture result'
    assert second['criticReviewId'] != critic_id and second['criticReview'] is None
    old = store.get(critic_id)
    assert old['status'] == 'cancelled' and old['work']['activeSession'] is None
    assert old['history'][-1]['action'] == 'critic_superseded'
    assert len(store.execution.ticks) == 0


def test_new_producer_work_invalidates_old_pass_even_without_definition_edit(store):
    """Stopping a fresh run cannot reuse the result/critic from an earlier producer session."""
    issue = store.create({'title': 'New work', **metadata()})
    review_ready(store, issue['id'])
    store.update(issue['id'], {'status':'ready'}, actor='user')
    keys = session(store, issue['id'], 'unreported-new-work')
    assert store.get(issue['id'])['criticReview'] is None
    store.execution.stop(issue['id'], keys)
    with pytest.raises(DomainError) as failure:
        store.update(issue['id'], {'status': 'done'}, actor='user')
    assert failure.value.code == 'stale_result'


def test_parent_completion_requires_all_ordinary_children_even_archived_ones(store):
    """A generated critic child does not create a completion cycle; ordinary unresolved children still block."""
    parent = store.create({'title': 'Parent', **metadata()})
    child = store.create({'title': 'Child', 'parentId': parent['id'], **metadata()})
    review_ready(store, parent['id'])
    with pytest.raises(DomainError) as failure:
        store.update(parent['id'], {'status': 'done'}, actor='user')
    assert failure.value.code == 'children_unfinished'
    finish_noncode(store, child['id'])
    # New child execution invalidates the parent's earlier acceptance, so review the current aggregate.
    assert store.get(parent['id'])['status'] == 'rework'
    assert review_ready(store, parent['id'])['status'] == 'done'


def test_archive_tombstone_restore_retains_history_and_never_deletes_worktree_files(store, tmp_path):
    """Only an archived terminal leaf can become a recoverable tombstone; filesystem data survives."""
    issue = store.create({'title': 'Cancelled task'})
    marker = tmp_path / 'managed-worktree'
    marker.mkdir()
    (marker / 'user-change').write_text('preserve')
    store._record_workflow(issue['id'], {'worktree': str(marker)}, 'fixture_worktree')
    with pytest.raises(DomainError):
        store.spaces.archive(issue['id'])
    store.update(issue['id'], {'status': 'cancelled'}, actor='user')
    store.spaces.archive(issue['id'])
    assert store.list() == [] and len(store.list(include_archived=True)) == 1
    assert store.spaces.delete(issue['id']) == {'id': issue['id'], 'deleted': True}
    with pytest.raises(DomainError):
        store.get(issue['id'])
    restored = store.spaces.restore(issue['id'])
    assert restored['status'] == 'cancelled' and restored['deletedAt'] is None and restored['archivedAt'] is None
    assert [item['action'] for item in restored['history']][-3:] == ['archived', 'deleted', 'restored']
    assert (marker / 'user-change').read_text() == 'preserve'


def test_terminal_parent_rejects_new_or_reparented_children(store):
    """No invisible active subtree can be attached after a human terminal decision."""
    parent = store.create({'title': 'Closed parent'})
    store.update(parent['id'], {'status': 'cancelled'}, actor='user')
    child = store.create({'title': 'Independent child'})
    with pytest.raises(DomainError):
        store.create({'title': 'New child', 'parentId': parent['id']})
    with pytest.raises(DomainError):
        store.update(child['id'], {'parentId': parent['id']})


def test_legacy_database_backfill_preserves_ids_comments_and_marks_unknown_work(tmp_path):
    """Migration adds structure without inventing sessions/results or changing historical identifiers."""
    with sqlite3.connect(tmp_path / 'issues.sqlite3') as connection:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO issues(id,title,description,status,priority,created_at,updated_at) VALUES('legacy','Existing','','in_progress','normal','before','before')")
        connection.execute("INSERT INTO comments(issue_id,text,actor,created_at) VALUES('legacy','Preserve','user','before')")
    store = IssueStore(tmp_path)
    try:
        value = store.get('legacy')
        assert value['id'] == 'legacy' and value['spaceId'] == 'default' and value['createdAt'] == 'before'
        assert value['status'] == 'rework' and value['work']['elapsedMs'] == 0 and value['currentResult'] is None
        assert value['comments'][0]['text'] == 'Preserve' and value['history'][-1]['action'] == 'legacy_work_suspended'
        assert store.db.connection.execute('PRAGMA user_version').fetchone()[0] == 6
    finally:
        store.close()


def test_heartbeat_commit_failure_keeps_elapsed_anchor(store):
    """SQLite rollback cannot reset monotonic accounting or charge the same interval twice."""
    ticks = [0]
    store.execution = Execution(store, clock=lambda: datetime(2026, 10, 22, tzinfo=timezone.utc), monotonic=lambda: ticks[0])
    issue = store.create({'title': 'Commit failure', **metadata()})
    keys = session(store, issue['id'])
    real = store.db.connection

    class Connection:
        """Fail one real COMMIT while allowing the production transaction wrapper to roll it back."""
        fail = True
        def execute(self, command, *arguments):
            if command == 'COMMIT' and self.fail:
                self.fail = False
                raise sqlite3.OperationalError('injected commit failure')
            return real.execute(command, *arguments)

    store.db.connection = Connection()
    try:
        ticks[0] = 30
        with pytest.raises(sqlite3.OperationalError):
            store.execution.heartbeat(issue['id'], keys)
        assert real.execute('SELECT elapsed_ms FROM work_sessions').fetchone()[0] == 0
        ticks[0] = 40
        assert store.execution.heartbeat(issue['id'], keys)['work']['elapsedMs'] == 40000
    finally:
        store.db.connection = real


def test_active_status_exit_and_definition_edit_freeze_accounted_time(store):
    """Generic board actions cannot leave an invisible running lease after the issue stops being in progress."""
    ticks = [0]
    store.execution = Execution(store, clock=lambda: datetime(2026, 10, 22, tzinfo=timezone.utc), monotonic=lambda: ticks[0])
    issue = store.create({'title': 'Active', **metadata()})
    session(store, issue['id'])
    ticks[0] = 10
    paused = store.update(issue['id'], {'status': 'blocked'})
    ticks[0] = 100
    assert paused['work']['elapsedMs'] == store.get(issue['id'])['work']['elapsedMs'] == 10000
    assert paused['work']['activeSession'] is None
    session(store, issue['id'], 'again')
    ticks[0] = 110
    changed = store.update(issue['id'], {'description': 'New acceptance scope'})
    assert changed['status'] == 'rework' and changed['work']['activeSession'] is None
    assert changed['work']['elapsedMs'] == 20000


def test_http_authority_workflow_identity_and_structured_rework(tmp_path):
    """Agent capability growth does not grant spaces, archive/delete or human merge UI actions."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        identity = client.get('/api/v1/status').json()
        assert identity['contractVersion'] == 'ag-tasks-workflow/v2' and identity['leaseSeconds'] == 90
        assert 'rework' in identity['statuses'] and 'critic' in identity['roles']
        issue = client.post('/api/v1/agent/issues', json={'title': 'Needs explicit definition'}).json()
        failed = client.post('/api/v1/agent/issues/' + issue['id'] + '/work/start', json={'agentId': 'worker', 'sessionKey': 'run'})
        assert failed.status_code == 409 and failed.json()['error']['details']['criterionFailures']
        assert client.get('/api/v1/issues/' + issue['id']).json()['status'] == 'rework'
        for path, body in [('/api/v1/spaces', {'name': 'No privilege'}), ('/api/v1/issues/' + issue['id'] + '/archive', {})]:
            assert client.post(path, json=body).status_code == 403
        assert client.delete('/api/v1/issues/' + issue['id']).status_code == 403
        assert client.get('/api/v1/issues/graph').status_code == 200


def test_compact_definition_result_and_graph_outputs_remain_byte_bounded(store):
    """Pages expose bounded previews/counts; immutable result evidence remains available in details."""
    definitions = [{'id': str(index), 'text': 'x' * 1000, 'passed': True, 'evidence': 'y' * 1000} for index in range(20)]
    issue = store.create({'title': 'Large metadata', 'intendedRole': 'developer', 'dor': definitions, 'dod': definitions})
    compact = store.list()[0]
    assert len(compact['dor']) == 3 and compact['dorCount'] == 20 and compact['dorTruncated']
    assert len(store.get(issue['id'])['dor']) == 20
    assert len(json.dumps(store.spaces.graph()).encode()) < 1000000


def test_mixed_parent_completion_and_blocking_cycles_are_rejected(store):
    """Visual composition does not reverse the scheduler's child-before-parent completion prerequisite."""
    parent = store.create({'title': 'Parent'})
    child = store.create({'title': 'Child', 'parentId': parent['id']})
    with pytest.raises(DomainError) as failure:
        store.link(parent['id'], child['id'], 'blocks')
    assert failure.value.code == 'dependency_cycle'
    assert store.link(child['id'], parent['id'], 'blocks')['links'][0]['kind'] == 'blocks'


def test_mixed_two_parent_path_and_reparent_admission_are_atomic(store):
    """A longer explicit/implicit prerequisite path cannot be closed by a new block or hierarchy move."""
    first = store.create({'title': 'First parent'})
    a = store.create({'title': 'First child', 'parentId': first['id']})
    second = store.create({'title': 'Second parent'})
    b = store.create({'title': 'Second child', 'parentId': second['id']})
    store.link(first['id'], b['id'], 'blocks')
    with pytest.raises(DomainError):
        store.link(second['id'], a['id'], 'blocks')
    independent = store.create({'title': 'Independent'})
    store.link(second['id'], independent['id'], 'blocks')
    with pytest.raises(DomainError):
        store.update(independent['id'], {'parentId': second['id']})
    assert store.get(independent['id'])['parentId'] is None


def test_current_critic_is_a_completion_prerequisite_for_cycle_admission(store):
    """The parent cannot block the very critic whose pass is required for its completion."""
    parent = store.create({'title': 'Review parent', **metadata()})
    keys = session(store, parent['id'])
    submitted = store.results.submit(parent['id'], {**keys, 'result': content()})
    with pytest.raises(DomainError):
        store.link(parent['id'], submitted['criticReviewId'], 'blocks')
