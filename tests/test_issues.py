"""Durable issue domain boundaries and append-only audit regressions."""

import pytest
from ag_tasks.issues import IssueStore, DomainError
from workflow_helpers import finish_noncode, metadata


@pytest.fixture
def store(tmp_path):
    """Keep every issue and database isolated from real user state."""
    value = IssueStore(tmp_path / 'state')
    yield value
    value.close()


def test_issue_crud_comments_links_and_persistence(store):
    first = store.create({'title': 'Investigate incident', 'externalRefs': [{'url': 'http://127.0.0.1:8890/incidents/abc', 'title': 'Evidence', 'kind': 'observer_incident'}]})
    second = store.create({'title': 'Related fix', 'parentId': first['id'], 'priority': 'high'})
    assert first['key'] == 'AGT-1' and second['key'] == 'AGT-2'
    store.link(first['id'], second['id'])
    store.comment(first['id'], 'Source evidence reviewed')
    changed = store.update(first['id'], {'status': 'ready', 'assignee': 'agent-1'})
    assert changed['links'] == [{'issueId': second['id'], 'kind': 'related'}]
    assert store.get(second['id'])['links'][0]['issueId'] == first['id']
    assert changed['comments'][0]['text'] == 'Source evidence reviewed'
    assert [item['action'] for item in changed['history']] == ['created', 'linked', 'commented', 'updated']
    assert len(store.list(parent_id=first['id'])) == 1
    assert 'history' not in store.list()[0]
    store.close()
    other = IssueStore(store.data_dir)
    try:
        assert other.get(first['id'])['externalRefs'][0]['kind'] == 'observer_incident'
    finally:
        other.close()


def test_hierarchy_cycles_self_relations_and_unknown_fields(store):
    root = store.create({'title': 'Parent'})
    child = store.create({'title': 'Child', 'parentId': root['id']})
    for patch in ({'parentId': child['id']}, {'parentId': root['id']}, {'branch': 'main'}, {'status': 'unknown'}):
        with pytest.raises(DomainError):
            store.update(root['id'], patch)
    with pytest.raises(DomainError):
        store.link(root['id'], root['id'])
    assert store.get(root['id'])['historyCount'] == 1


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:secret@example.com', 'https://example.com:99999', 'https://example.com/has space'])
def test_external_refs_are_safe_links_not_command_inputs(store, url):
    with pytest.raises(DomainError):
        store.create({'title': 'Bad ref', 'externalRefs': [{'url': url}]})
    assert store.list() == []


def test_agent_cannot_complete_cancel_or_mutate_git_state(store):
    item = store.create({'title': 'Needs human decision'})
    for status in ('done', 'cancelled'):
        with pytest.raises(DomainError) as error:
            store.update(item['id'], {'status': status})
        assert error.value.status == 403
    done = finish_noncode(store, item['id'])
    assert done['status'] == 'done'


def test_audit_and_comment_collections_are_bounded_but_retained(store):
    item = store.create({'title': 'Long lived'})
    for index in range(210):
        store.comment(item['id'], 'Comment ' + str(index))
    result = store.get(item['id'])
    assert len(result['comments']) == 20 and result['commentsCount'] == 210
    assert len(result['history']) == 20 and result['historyCount'] == 211
    assert result['comments'][-1]['text'] == 'Comment 209'


def test_data_owner_lock_blocks_second_instance_and_releases_on_close(store):
    with pytest.raises(OSError):
        IssueStore(store.data_dir)
    directory = store.data_dir
    store.close()
    reopened = IssueStore(directory)
    reopened.close()


def test_directed_blocks_prevent_start_and_cycles(store):
    first = store.create({'title': 'Blocker'})
    second = store.create({'title': 'Blocked'})
    store.link(first['id'], second['id'], kind='blocks')
    assert store.get(first['id'])['links'] == [{'issueId': second['id'], 'kind': 'blocks'}]
    assert store.get(second['id'])['links'] == [{'issueId': first['id'], 'kind': 'blocked_by'}]
    with pytest.raises(DomainError):
        store.link(second['id'], first['id'], kind='blocks')
    with pytest.raises(DomainError):
        store.update(second['id'], {'status': 'in_progress'})
    finish_noncode(store, first['id'])
    store.update(second['id'], metadata())
    assert store.execution.start(second['id'], {'agentId': 'worker', 'sessionKey': 'after-blocker'})['status'] == 'in_progress'


@pytest.mark.parametrize('patch', [{'parentId': []}, {'repoId': {}}, {'parentId': True}])
def test_malformed_identifiers_are_domain_errors(store, patch):
    with pytest.raises(DomainError) as error:
        store.create({'title': 'Malformed', **patch})
    assert error.value.status == 400


def test_compact_pages_and_large_retained_history_fit_transport(store):
    import json
    refs = [{'url': 'https://example.invalid/' + 'a' * 1800, 'title': 'evidence'} for _ in range(20)]
    issue = store.create({'title': 'Large issue', 'description': 'x' * 16000, 'externalRefs': refs})
    for index in range(25):
        store.update(issue['id'], {'description': str(index) + 'x' * 16000, 'externalRefs': refs})
    page = store.list()
    assert len(page[0]['description']) == 512 and len(page[0]['externalRefs']) == 2
    assert page[0]['descriptionTruncated'] and page[0]['externalRefsCount'] == 20
    full = store.get(issue['id'])
    assert len(full['externalRefs']) == 20 and full['historyTruncated']
    assert full['historyCount'] == 26 and len(json.dumps(full).encode()) < 1000000
    store.update(issue['id'], {'priority': 'urgent'})
    assert len(store.get(issue['id'])['externalRefs']) == 20  # A compact list cannot truncate durable state.


@pytest.mark.parametrize('identifier', [[], {}, True])
def test_malformed_relation_identifier_is_domain_error(store, identifier):
    issue = store.create({'title': 'Valid source'})
    with pytest.raises(DomainError) as error:
        store.link(issue['id'], identifier)
    assert error.value.status == 400


def test_reference_error_locates_bad_item_without_echoing_private_input(store):
    """Direct HTTP/domain callers receive the same safe contract guidance as the MCP preflight."""
    with pytest.raises(DomainError) as error:
        store.create({'title': 'Bad refs', 'externalRefs': [
            {'url': 'https://example.invalid/evidence'}, {'url': 'https://user:secret@example.invalid'}]})
    assert error.value.code == 'invalid_references'
    assert error.value.details['field'] == 'externalRefs[1].url'
    assert 'secret' not in str(error.value) and 'secret' not in str(error.value.details)
    assert store.list() == []
