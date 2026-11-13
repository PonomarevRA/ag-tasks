"""Review handoff acceptance exercises actual immutable result and queue operations."""

import pytest
from fastapi.testclient import TestClient
from ag_tasks.app import create_app
from ag_tasks.issues import IssueStore, DomainError
from workflow_helpers import metadata


@pytest.fixture
def store(tmp_path):
    """Use private data and no external worker/model."""
    value = IssueStore(tmp_path)
    yield value
    value.close()


def submit(store, issue=None, key='initial'):
    """Submit checked fixture work through normal lease and result admission."""
    issue = issue or store.create({'title': 'Deliverable', **metadata()})
    active = store.execution.start(issue['id'], {'agentId': 'producer', 'sessionKey': key})
    return store.results.submit(issue['id'], {'agentId': 'producer', 'sessionKey': key, 'sessionId': active['work']['activeSession']['sessionId'],
        'result': {'summary': 'Result', 'changes': ['change'], 'validation': ['checked'], 'limitations': '', 'artifacts': []}})


def lane(store):
    """A declared queue profile is only metadata, not a qualified or launched provider."""
    return store.service.queue.create({'laneId': 'reviews', 'title': 'Critic', 'profile': {'id': 'critic', 'version': '1', 'protocol': 'acp', 'protocolVersion': 1}})


def test_current_inbox_pages_oldest_independently_of_issue_list(store):
    first, second = submit(store), submit(store)
    for number in range(205):
        store.create({'title': f'Newer backlog {number}'})
    page = store.reviews.list(limit=1)
    assert page['items'][0]['id'] == first['criticReviewId']
    assert page['items'][0]['handoff']['state'] == 'reviewer_required'
    assert page['hasMore'] and page['nextOffset'] == 1
    assert store.reviews.list(limit=1, offset=1)['items'][0]['id'] == second['criticReviewId']
    store.execution.start(first['id'], {'agentId': 'producer', 'sessionKey': 'rework'})
    assert [i['id'] for i in store.reviews.list()['items']] == [second['criticReviewId']]


def test_assignment_is_distinct_idempotent_and_fenced_to_owner(store):
    result = submit(store)
    lane(store)
    with pytest.raises(DomainError) as error:
        store.reviews.assign(result['criticReviewId'], 'producer', 'reviews')
    assert error.value.code == 'distinct_critic_required'
    assigned = store.reviews.assign(result['criticReviewId'], 'critic', 'reviews')
    assert assigned['state'] == 'queued'
    assert store.reviews.assign(result['criticReviewId'], 'critic', 'reviews') == assigned
    assert len(store.service.queue.items('reviews')['items']) == 1
    with pytest.raises(DomainError) as error:
        store.execution.start(result['criticReviewId'], {'agentId': 'another', 'sessionKey': 'wrong'})
    assert error.value.code == 'reviewer_assignment_mismatch'
    assert store.get(result['criticReviewId'])['work']['state'] == 'never_started'
    assert store.execution.start(result['criticReviewId'], {'agentId': 'critic', 'sessionKey': 'right'})['work']['activeSession']


def test_future_route_enqueues_once_and_superseded_generation_cannot_dispatch(store):
    lane(store)
    old = submit(store)
    store.reviews.configure('default', 'critic', 'reviews')
    assert store.reviews.handoff(old['criticReviewId'])['state'] == 'reviewer_required'
    result = submit(store)
    assigned = store.reviews.handoff(result['criticReviewId'])
    assert assigned['state'] == 'queued'
    item = store.service.queue.item(assigned['queueItemId'])
    assert result['criticReviewId'] in item['text'] and 'definitionRevision' in item['text']
    submit(store, result, key='generation-two')
    assert store.service.queue.item(item['id'])['state'] == 'cancelled'
    assert len(store.reviews.list()['items']) == 2  # old and current generation, never superseded review


def test_bad_default_cannot_discard_producer_result(store):
    store.reviews.configure('default', 'producer')
    result = submit(store)
    assert result['status'] == 'in_review' and result['resultsCount'] == 1
    assert store.reviews.handoff(result['criticReviewId'])['state'] == 'reviewer_required'
    critic = store.get(result['criticReviewId'])
    assert any(e['action'] == 'review_handoff_failed' for e in critic['history'])


def test_assignment_routes_require_user_action_token_but_inbox_is_readable(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8895') as client:
        result = submit(app.state.store)
        assert client.get('/api/v1/issues/reviews').json()['items'][0]['id'] == result['criticReviewId']
        path = f"/api/v1/issues/{result['criticReviewId']}/review-assignment"
        assert client.post(path, json={'reviewerId': 'critic'}).status_code == 403
        token = {'X-Ag-Tasks-UI': client.get('/api/v1/ui-session').json()['token']}
        assert client.post(path, json={'reviewerId': 'critic'}, headers=token).json()['state'] == 'assigned'
        assert client.post('/api/v1/spaces/default/review-route', json={'reviewerId': 'critic'}, headers=token).status_code == 200


def test_live_reviewer_cannot_be_reassigned(store):
    """Assignment never steals an already accepted critic session."""
    result = submit(store)
    active = store.execution.start(result['criticReviewId'], {'agentId': 'original-critic', 'sessionKey': 'active'})
    with pytest.raises(DomainError) as error:
        store.reviews.assign(result['criticReviewId'], 'new-critic')
    assert error.value.code == 'work_already_active'
    assert store.get(active['id'])['assignee'] is None


@pytest.mark.parametrize('verdict', ['pass', 'rework'])
def test_manual_review_report_cancels_waiting_worker_intent(store, verdict):
    """A completed/rejected generation must never launch later from a still-waiting queue item."""
    result = submit(store)
    lane(store)
    handoff = store.reviews.assign(result['criticReviewId'], 'critic', 'reviews')
    active = store.execution.start(result['criticReviewId'], {'agentId': 'critic', 'sessionKey': 'review'})
    parent = store.results.report(active['id'], {'agentId': 'critic', 'sessionKey': 'review', 'sessionId': active['work']['activeSession']['sessionId'],
        'generation': result['resultGeneration'], 'definitionRevision': result['definitionRevision'], 'reviewedHead': result['reviewedHead'],
        'verdict': verdict, 'summary': 'Independent declaration', 'findings': ['Fix fixture boundary'] if verdict == 'rework' else [], 'evidence': 'Inspected fixture'})
    assert parent['status'] == ('done' if verdict == 'pass' else 'rework')
    assert store.service.queue.item(handoff['queueItemId'])['state'] == 'cancelled'


def test_cancelled_or_mistyped_handoff_can_be_released_with_cas(store):
    """Safe correction cannot steal any active or uncertain worker reservation."""
    result = submit(store)
    lane(store)
    old = store.reviews.assign(result['criticReviewId'], 'typo', 'reviews')
    with pytest.raises(DomainError) as error:
        store.reviews.release(result['criticReviewId'], 'wrong-fence')
    assert error.value.code == 'handoff_conflict'
    assert store.reviews.release(result['criticReviewId'], old['assignedAt'])['state'] == 'reviewer_required'
    assert store.service.queue.item(old['queueItemId'])['state'] == 'cancelled'
    new = store.reviews.assign(result['criticReviewId'], 'critic', 'reviews')
    assert new['queueItemId'] != old['queueItemId']
    active = store.execution.start(result['criticReviewId'], {'agentId': 'critic', 'sessionKey': 'review'})
    with pytest.raises(DomainError) as error:
        store.reviews.release(result['criticReviewId'], new['assignedAt'])
    assert error.value.code == 'work_already_active'


def test_superseded_claimed_review_requests_cancel_and_retains_reservation(store):
    """A claimed intent cannot dispatch after its critic was superseded; no runtime release is fabricated."""
    result = submit(store)
    lane(store)
    handoff = store.reviews.assign(result['criticReviewId'], 'critic', 'reviews')
    attempt = store.service.queue.worker.claim({'laneId': 'reviews', 'workerId': 'worker', 'workerEpoch': 'epoch'})
    submit(store, result, key='superseding')
    assert store.service.queue.item(handoff['queueItemId'])['state'] == 'cancel_requested'
    assert store.service.queue.get('reviews')['activeAttempt']['id'] == attempt['id']
    with pytest.raises(DomainError) as error:
        store.service.queue.worker.dispatch({'attemptId': attempt['id'], 'workerId': 'worker', 'workerEpoch': 'epoch', 'fence': attempt['fence']})
    assert error.value.code == 'dispatch_cancelled'


def test_review_progress_stays_consistent_across_expiry_assignment_and_new_generation(store):
    """Every UI view reads one reason and the same immutable result/review reference."""
    result = submit(store)
    producer, critic = result['id'], result['criticReviewId']

    def progress(reason):
        value = store.get(producer)['reviewProgress']
        assert value == store.get(critic)['reviewProgress'] == store.reviews.list()['items'][0]['reviewProgress']
        assert value['reason'] == reason
        assert value['generation'] == result['resultGeneration']
        assert value['nextAction']['issueId'] == critic
        assert 'reviewProgress' not in store.list()[0]
        return value

    pending = progress('reviewer_required')
    active = store.execution.start(critic, {'agentId': 'first', 'sessionKey': 'attempt'})
    assert progress('active')['reviewerId'] == 'first'
    assert store.reviews.metrics()['unassigned'] == 0
    store.execution.ticks[active['work']['activeSession']['sessionId']] -= 91
    assert progress('reviewer_required')['lastAttemptExpired']
    assert store.reviews.metrics()['unassigned'] == 1
    handoff = store.reviews.assign(critic, 'second')
    assert progress('assigned')['waitingSince'] == pending['waitingSince']
    store.reviews.release(critic, handoff['assignedAt'])
    lane(store)
    store.reviews.assign(critic, 'third', 'reviews')
    assert progress('queued')['lastAttemptExpired']
    updated = submit(store, result, key='fresh')
    assert store.get(critic)['reviewProgress'] is None
    assert store.reviews.list()['items'][0]['id'] == updated['criticReviewId']
