"""Fixture-only explicit work/result/critic steps; never bypass production admission guards."""

from uuid import uuid4


def definition():
    """One explicit caller-reported criterion with nonempty evidence."""
    return [{'id': 'checked', 'text': 'Fixture acceptance checked', 'passed': True, 'evidence': 'Deterministic test fixture'}]


def metadata():
    """Ready test metadata still has no work session or fabricated elapsed time."""
    return {'intendedRole': 'developer', 'dor': definition(), 'dod': definition()}


def review_ready(store, issue_id):
    """Exercise real producer/critic APIs to reach automatic completion with exact current provenance."""
    issue = store.get(issue_id)
    if issue['intendedRole'] is None or not issue['dor'] or not issue['dod']:
        issue = store.update(issue_id, metadata())
    if issue['repoId']:
        issue = store.update(issue_id, {'languages': ['Python'], 'systems': ['Fixture system']})
    key = uuid4().hex
    active = store.execution.start(issue_id, {'agentId': 'fixture-producer', 'sessionKey': key})
    session = {'agentId': 'fixture-producer', 'sessionKey': key, 'sessionId': active['work']['activeSession']['sessionId']}
    content = {'summary': 'Fixture result', 'changes': ['Fixture change'], 'validation': ['Deterministic assertions'], 'limitations': 'Fixture only', 'artifacts': []}
    parent = store.results.submit(issue_id, {**session, 'result': content})
    key = uuid4().hex
    critic = store.execution.start(parent['criticReviewId'], {'agentId': 'fixture-critic', 'sessionKey': key})
    report = {'agentId': 'fixture-critic', 'sessionKey': key, 'sessionId': critic['work']['activeSession']['sessionId'],
              'generation': parent['resultGeneration'], 'definitionRevision': parent['definitionRevision'], 'reviewedHead': parent['currentResult']['reviewedHead'],
              'verdict': 'pass', 'summary': 'Fixture independently reviewed', 'findings': [], 'evidence': 'Fixture critic declaration'}
    return store.results.report(critic['id'], report)


def finish_noncode(store, issue_id):
    """A repeated completion request is idempotent after explicit current critic evidence."""
    review_ready(store, issue_id)
    return store.update(issue_id, {'status': 'done'}, actor='user')
