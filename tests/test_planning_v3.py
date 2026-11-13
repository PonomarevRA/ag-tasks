"""Planning acceptance: persistent identities, safe Markdown, explicit workflow and lossless ZIP content."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from ag_tasks.app import create_app
from ag_tasks.documents import render_markdown
from ag_tasks.issues import DomainError, IssueStore
from workflow_helpers import metadata as ready, review_ready as submit


@pytest.fixture
def store(tmp_path):
    value = IssueStore(tmp_path)
    yield value
    value.close()


def test_prefixes_number_per_space_keep_aliases_and_never_reuse(store):
    old = store.create({'title': 'Existing'})
    hp = store.spaces.write({'name': 'example-portfolio', 'prefix': 'hp'})
    calc = store.spaces.write({'name': 'Calculator', 'prefix': 'CALCULATOR'})
    first = store.create({'title': 'First', 'spaceId': hp['id']})
    second = store.create({'title': 'Second', 'spaceId': hp['id']})
    elsewhere = store.create({'title': 'Other', 'spaceId': calc['id']})
    assert (old['key'], first['key'], second['key'], elsewhere['key']) == ('AGT-1', 'HP-1', 'HP-2', 'CALCULATOR-1')
    with pytest.raises(DomainError, match='prefix'):
        store.spaces.write({'name': 'Duplicate', 'prefix': 'hP'})
    assert len(store.spaces.list()) == 3
    store.spaces.write({'name': 'Portfolio', 'prefix': 'PORT'}, hp['id'])
    assert store.get('hp-1')['id'] == first['id'] and store.get(first['id'])['key'] == 'PORT-1'
    assert {'HP-1', 'PORT-1'} <= set(store.get(first['id'])['aliases'])
    with pytest.raises(DomainError):
        store.spaces.write({'name': 'Recycled', 'prefix': 'HP'})
    moved = store.update(second['id'], {'spaceId': calc['id']})
    assert moved['key'] == 'CALCULATOR-2' and store.get('HP-2')['id'] == moved['id']


def test_cross_space_blocks_are_real_and_graph_has_incoming_boundary_stubs(store):
    one = store.spaces.write({'name': 'Portfolio', 'prefix': 'HP'})
    two = store.spaces.write({'name': 'Calculation', 'prefix': 'CALC'})
    source = store.create({'title': 'Source', 'spaceId': one['id'], **ready()})
    target = store.create({'title': 'Target', 'spaceId': two['id'], **ready()})
    store.link(source['id'], target['id'], 'blocks')
    with pytest.raises(DomainError) as error:
        store.execution.start(target['id'], {'agentId': 'dev', 'sessionKey': 'first'})
    assert error.value.code == 'issue_blocked'
    graph = store.spaces.graph(two['id'])
    assert graph['edges'] == [{'from': source['id'], 'to': target['id'], 'kind': 'blocks'}]
    assert graph['stubs'][0]['key'] == 'HP-1' and graph['stubs'][0]['spaceId'] == one['id']
    with pytest.raises(DomainError) as error:
        store.link(target['id'], source['id'], 'blocks')
    assert error.value.code == 'dependency_cycle'


def test_plan_is_atomic_and_only_explicit_role_decomposition_creates_work(store):
    with pytest.raises(DomainError):
        store.planning.create_plan({'title': 'Bad', 'markdown': 'x' * 50000})
    assert store.list() == []
    issue = store.planning.create_plan({'title': 'Research', 'markdown': '# Goal\n\n- Inspect\n- Test',
                                      'estimatedComplexity': {'points': 5, 'reason': 'New storage contract'}})
    assert issue['kind'] == 'rnd' and issue['work']['elapsedMs'] == 0
    assert issue['documents'][0]['name'] == 'plan.md' and not store.list(parent_id=issue['id'])
    with pytest.raises(DomainError) as error:
        store.spaces.decompose(issue['id'], {'children': [{'title': 'Unowned'}]})
    assert error.value.code == 'role_required' and not store.list(parent_id=issue['id'])
    child = store.spaces.decompose(issue['id'], {'children': [{'title': 'Implement', 'intendedRole': 'developer'}, {'title': 'Test', 'intendedRole': 'qa'}]})
    assert len(child['items']) == 2 and all(item['work']['state'] == 'never_started' for item in child['items'])


def test_markdown_documents_are_safe_revisioned_and_plan_changes_stop_work(store):
    issue = store.create({'title': 'Work', **ready()})
    file = store.documents.save(issue['id'], {'name': 'plan.md', 'purpose': 'plan', 'content': '# Plan'})
    started = store.execution.start(issue['id'], {'agentId': 'producer', 'sessionKey': 'lease'})
    with pytest.raises(DomainError) as error:
        store.documents.save(issue['id'], {'name': 'plan.md', 'content': 'overwrite', 'purpose': 'plan'})
    assert error.value.code == 'document_conflict'
    store.documents.save(issue['id'], {'name': 'plan.md', 'content': 'Updated', 'purpose': 'plan', 'expectedRevision': file['revision']})
    current = store.get(issue['id'])
    assert current['status'] == 'rework' and current['work']['activeSession'] is None
    assert current['definitionRevision'] == started['definitionRevision'] + 1
    for name in ['../bad.md', '/tmp/bad.md', 'bad.html', '.hidden.md', 'x\\bad.md']:
        with pytest.raises(DomainError):
            store.documents.save(issue['id'], {'name': name, 'content': 'x'})
    html = render_markdown('# Heading\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n![track](https://example.org/track.png)\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```python\nprint("safe")\n```')
    assert '<h1>Heading</h1>' in html and '<table>' in html and '<pre><code' in html
    assert '<script>' not in html and '<img' not in html and 'href="javascript:' not in html


def test_problem_resolution_and_actual_complexity_require_evidence(store):
    issue = store.create({'title': 'Implementation', **ready()})
    problem = store.planning.problem(issue['id'], {'title': 'Unexpected dependency', 'details': 'A reproducible timeout', 'severity': 'high'})
    with pytest.raises(DomainError):
        store.planning.problem(issue['id'], {'state': 'resolved'}, problem_id=problem['id'])
    store.planning.problem(issue['id'], {'state': 'resolved', 'resolution': 'Bounded the request'}, problem_id=problem['id'])
    assert store.get(issue['id'])['problems']['items'][0]['resolution'] == 'Bounded the request'
    with pytest.raises(DomainError) as error:
        store.planning.record_actual(issue['id'], {'points': 5, 'reason': 'More unknowns'})
    assert error.value.code == 'result_required'
    submit(store, issue['id'])
    result = store.planning.record_actual(issue['id'], {'points': 5, 'reason': 'More unknowns'})
    assert result['actualComplexity']['points'] == 5 and result['actualComplexity']['resultGeneration'] == result['resultGeneration']
    for points in [True, 0, 4, 14, '5']:
        with pytest.raises(DomainError):
            store.planning.record_actual(issue['id'], {'points': points, 'reason': 'Invalid'})


def test_stale_work_cannot_be_reopened_by_status_patch_or_decomposed(store):
    issue = store.create({'title': 'Old work', **ready()})
    store.execution.start(issue['id'], {'agentId': 'producer', 'sessionKey': 'old'})
    stale = store.update(issue['id'], {'status': 'stale'})
    assert stale['work']['activeSession'] is None
    for operation in [lambda: store.execution.start(issue['id'], {'agentId': 'producer', 'sessionKey': 'new'}),
                      lambda: store.spaces.decompose(issue['id'], {'children': [{'title': 'Child'}]}),
                      lambda: store.update(issue['id'], {'status': 'ready'}, actor='user')]:
        with pytest.raises(DomainError):
            operation()
    reopened = store.planning.revalidate(issue['id'], {'reason': 'Requirements reviewed by user'})
    assert reopened['status'] == 'backlog' and all(not item['passed'] for item in reopened['dor'])


def olden(store, issue_id):
    """Age content without sleeps or changing persistent user state."""
    with store.db.transaction() as connection:
        connection.execute('UPDATE issues SET updated_at=? WHERE id=?', ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), issue_id))


def test_zip_roundtrip_preserves_content_ids_relations_and_status_after_restart(tmp_path):
    store = IssueStore(tmp_path)
    issue = store.planning.create_plan({'title': 'Old plan', 'markdown': '# Plan\n' + 'Repeat\n' * 1000})
    file = issue['documents'][0]
    store.comment(issue['id'], 'Keep this comment')
    store.planning.problem(issue['id'], {'title': 'Storage', 'details': 'Keep details'})
    child = store.create({'title': 'Child', 'parentId': issue['id']})
    before = store.get(issue['id'])
    olden(store, issue['id'])
    assert store.cold.sweep() == [issue['id']]
    cold = next(item for item in store.list() if item['id'] == issue['id'])
    assert cold['storage']['tier'] == 'zip' and cold['documents'][0]['id'] == file['id']
    filename = store.db.connection.execute('SELECT storage_zip FROM issues WHERE id=?', (issue['id'],)).fetchone()[0]
    archive = tmp_path / 'task-archives' / filename
    assert archive.exists() and archive.stat().st_size < 7000
    assert store.db.connection.execute('SELECT content FROM documents WHERE id=?', (file['id'],)).fetchone()[0] == ''
    store.close()
    store = IssueStore(tmp_path)
    try:
        restored = store.get(issue['id'])
        assert restored['storage']['tier'] == 'hot' and restored['status'] == before['status']
        assert restored['comments'][0]['text'] == 'Keep this comment'
        assert restored['problems']['items'][0]['details'] == 'Keep details'
        assert store.documents.get(issue['id'], file['id'])['content'].startswith('# Plan\nRepeat')
        assert store.get(child['id'])['parentId'] == issue['id']
        assert [item['action'] for item in restored['history']][-2:] == ['storage_compressed', 'storage_restored']
    finally:
        store.close()


def test_zip_failure_or_checksum_damage_never_silently_loses_task_data(store, monkeypatch):
    issue = store.create({'title': 'Old', 'description': 'Original content'})
    olden(store, issue['id'])
    original_replace = Path.replace
    monkeypatch.setattr(Path, 'replace', lambda *args: (_ for _ in ()).throw(OSError('Simulated write failure')))
    with pytest.raises(OSError):
        store.cold.sweep()
    assert store.db.connection.execute('SELECT description FROM issues WHERE id=?', (issue['id'],)).fetchone()[0] == 'Original content'
    monkeypatch.setattr(Path, 'replace', original_replace)
    assert store.cold.sweep() == [issue['id']]
    filename = store.db.connection.execute('SELECT storage_zip FROM issues WHERE id=?', (issue['id'],)).fetchone()[0]
    (store.data_dir / 'task-archives' / filename).write_bytes(b'corrupted')
    with pytest.raises(DomainError) as error:
        store.get(issue['id'])
    assert error.value.code == 'archive_unavailable'
    assert store.db.connection.execute('SELECT storage_zip FROM issues WHERE id=?', (issue['id'],)).fetchone()[0] == filename


def test_zip_excludes_real_work_and_concurrent_restore_is_idempotent(store):
    active = store.create({'title': 'Running', **ready()})
    store.execution.start(active['id'], {'agentId': 'dev', 'sessionKey': 'real'})
    olden(store, active['id'])
    assert store.cold.sweep() == []
    inactive = store.create({'title': 'Idle', 'description': 'Retain'})
    olden(store, inactive['id'])
    store.cold.sweep()
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda _: store.get(inactive['id']), range(8)))
    assert all(item['description'] == 'Retain' for item in values)
    assert store.db.connection.execute("SELECT COUNT(*) FROM history WHERE issue_id=? AND action='storage_restored'", (inactive['id'],)).fetchone()[0] == 1


def test_http_mcp_authority_and_alias_paths(tmp_path, monkeypatch):
    opened = []
    if os.name == 'nt':
        monkeypatch.setattr(os, 'startfile', opened.append)
    else:
        monkeypatch.setattr('ag_tasks.documents.subprocess.Popen', lambda args, **kwargs: opened.append(args[-1]))
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        token = {'X-Ag-Tasks-UI': client.get('/api/v1/ui-session').json()['token']}
        space = client.post('/api/v1/spaces', headers=token, json={'name': 'Portfolio', 'prefix': 'HP'}).json()
        plan = client.post('/api/v1/agent/plans', json={'title': 'Plan', 'markdown': '# Heading', 'spaceId': space['id']}).json()
        assert plan['key'] == 'HP-1' and plan['kind'] == 'rnd'
        assert client.patch('/api/v1/agent/issues/HP-1', json={'title': 'New title'}).json()['title'] == 'New title'
        doc = plan['documents'][0]
        assert client.get(f"/api/v1/issues/HP-1/documents/{doc['id']}").json()['content'] == '# Heading'
        assert client.post('/api/v1/issues/HP-1/open-folder', json={}).status_code == 403
        assert client.post('/api/v1/agent/issues/HP-1/open-folder', json={}).status_code in (404,405)
        assert client.post('/api/v1/issues/HP-1/open-folder', json={}, headers=token).status_code == 200
        assert len(opened) == 1 and Path(opened[0]).is_relative_to(tmp_path)
        client.patch('/api/v1/agent/issues/HP-1', json={'status': 'stale'})
        assert client.post('/api/v1/agent/issues/HP-1/revalidate', json={'reason': 'Agent bypass'}).status_code in (404,405)
        assert client.post('/api/v1/issues/HP-1/revalidate', headers=token, json={'reason': 'User review'}).json()['status'] == 'backlog'
        assert client.get(f"/api/v1/issues/HP-1/documents/{doc['id']}/download").text == '# Heading'


def test_stale_parent_stops_real_child_work_and_does_not_revalidate_it_implicitly(store):
    parent = store.create({'title': 'Old plan'})
    child = store.create({'title': 'Child', 'parentId': parent['id'], **ready()})
    store.execution.start(child['id'], {'agentId': 'dev', 'sessionKey': 'active-child'})
    store.update(parent['id'], {'status': 'stale'})
    assert store.get(child['id'])['status'] == 'stale'
    assert store.get(child['id'])['work']['activeSession'] is None
    store.planning.revalidate(parent['id'], {'reason': 'Parent relevant again'})
    assert store.get(child['id'])['status'] == 'stale'


def test_plan_downgrade_does_not_bypass_acceptance_invalidation(store):
    issue = store.create({'title': 'Planned', **ready()})
    doc = store.documents.save(issue['id'], {'name': 'plan.md', 'content': '# Plan', 'purpose': 'plan'})
    before = store.get(issue['id'])
    store.documents.save(issue['id'], {'name': 'plan.md', 'content': 'Changed', 'purpose': 'attachment', 'expectedRevision': doc['revision']})
    assert store.get(issue['id'])['definitionRevision'] == before['definitionRevision'] + 1
    for name in ['CON.md', 'lpt1.md', 'AUX.md', '文' * 90 + '.md']:
        with pytest.raises(DomainError):
            store.documents.save(issue['id'], {'name': name, 'content': 'x'})
    assert 'href="https://user:password@' not in render_markdown('[x](https://user:password@example.org/)')
    assert isinstance(render_markdown('[x](https://[invalid)'), str)


def test_parallel_creation_reserves_distinct_space_keys(store):
    hp = store.spaces.write({'name': 'Portfolio', 'prefix': 'HP'})
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(lambda index: store.create({'title': f'Task {index}', 'spaceId': hp['id']}), range(20)))
    assert {item['key'] for item in items} == {f'HP-{index}' for index in range(1,21)}


def test_oversized_archive_candidate_does_not_starve_following_tasks(store):
    large = store.create({'title': 'Large history'})
    small = store.create({'title': 'Normal'})
    olden(store, large['id']); olden(store, small['id'])
    with store.db.transaction() as connection:
        connection.executemany('INSERT INTO comments(issue_id,text,actor,created_at) VALUES(?,?,?,?)',
                               [(large['id'], 'Retain', 'fixture', 'before')] * 10001)
    assert store.cold.sweep() == []
    assert store.db.connection.execute('SELECT storage_retry_at FROM issues WHERE id=?', (large['id'],)).fetchone()[0]
    assert store.cold.sweep() == [small['id']]
    assert store.get(large['id'])['commentsCount'] == 10001


def test_zip_restores_immutable_result_payload_and_critic_history(store):
    issue = store.create({'title': 'Implemented', **ready()})
    submit(store, issue['id'])
    store.update(issue['id'], {'status': 'done'}, actor='user')
    before = store.get(issue['id'])
    olden(store, issue['id'])
    assert store.cold.sweep() == [issue['id']]
    after = store.get(issue['id'])
    assert after['currentResult'] == before['currentResult']
    assert after['criticReview'] == before['criticReview']
    assert after['status'] == 'done'


def test_open_folder_preserves_external_edits_and_does_not_make_redundant_copies(store):
    issue = store.create({'title': 'Files'})
    doc = store.documents.save(issue['id'], {'name': 'notes.md', 'content': 'Original'})
    directory = store.documents.folder(issue['id'])
    (directory / 'notes.md').write_text('Edited outside Tasks')
    store.documents.save(issue['id'], {'name': 'notes.md', 'content': 'Service version', 'expectedRevision': doc['revision']})
    store.documents.folder(issue['id'])
    assert (directory / 'notes.md').read_text() == 'Service version'
    copies = list(directory.glob('*.local-*.md'))
    assert len(copies) == 1 and copies[0].read_text() == 'Edited outside Tasks'
    store.documents.folder(issue['id'])
    assert len(list(directory.glob('*.local-*.md'))) == 1
