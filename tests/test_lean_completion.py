"""Automatic closure preserves independent evidence, exact human merge and retained difficulties."""

from datetime import datetime,timedelta,timezone
import pytest
from fastapi.testclient import TestClient
from ag_tasks.app import create_app
from ag_tasks.issues import IssueStore,DomainError
from ag_tasks.problem_digest import read
from workflow_helpers import metadata,review_ready


@pytest.fixture
def store(tmp_path):
    """One isolated database exercises normal migrations and completion without user data."""
    value=IssueStore(tmp_path/'state')
    try:yield value
    finally:value.close()


def test_critic_pass_closes_work_without_human_patch_or_fake_result(store):
    """Only a real current producer and distinct critic report establish completion."""
    issue=store.create({'title':'Complete',**metadata()})
    done=review_ready(store,issue['id'])
    assert done['status']=='done' and done['criticReview']['verdict']=='pass'
    assert done['work']['activeSession'] is None and done['resultsCount']==1
    assert done['history'][-1]['action']=='auto_completed'
    assert done['history'][-1]['data']['mergeApproved'] is False
    with pytest.raises(DomainError):
        store.execution.start(issue['id'],{'agentId':'same','sessionKey':'not-reopened'})


def test_pure_plan_rollup_waits_for_current_reviews_and_reopens_on_child_change(store):
    """A planning container has no fabricated execution interval or producer result."""
    parent=store.create({'title':'Plan','kind':'rnd'})
    first=store.create({'title':'A','parentId':parent['id'],**metadata()})
    second=store.create({'title':'B','parentId':parent['id'],**metadata()})
    review_ready(store,first['id'])
    assert store.get(parent['id'])['status']!='done'
    review_ready(store,second['id'])
    done=store.get(parent['id'])
    assert done['status']=='done' and done['resultsCount']==0 and done['work']['elapsedMs']==0
    store.update(first['id'],{'status':'ready'},actor='user')
    assert store.get(parent['id'])['status']=='rework'
    review_ready(store,first['id'])
    assert store.get(parent['id'])['status']=='done'


def test_unmet_parent_criteria_block_rollup(store):
    """Aggregate completion cannot tick custom user criteria just because the last child passed."""
    parent=store.create({'title':'Plan','kind':'rnd','dod':[{'id':'additional','text':'Additional scope','passed':False,'evidence':''}]})
    child=store.create({'title':'A','parentId':parent['id'],**metadata()})
    review_ready(store,child['id'])
    assert store.get(parent['id'])['status']!='done'
    assert store.get(parent['id'])['lifecycle']['phase']=='completion_pending'


def test_problem_digest_survives_zip_without_restoring_and_preserves_declared_evidence(store):
    """Compact hot summaries retain agent difficulties/solutions while full content remains in ZIP."""
    issue=store.create({'title':'Old task'})
    store.planning.problem(issue['id'],{'title':'Parser failure','details':'d'*3000,'state':'resolved','resolution':'verified fix '*200})
    old=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
    store.db.connection.execute('UPDATE issues SET updated_at=? WHERE id=?',(old,issue['id']))
    assert store.cold.sweep(limit=1)==[issue['id']]
    digest=read(store)
    item=digest['items'][0]
    assert item['details']=='d'*1024 and item['resolution'].startswith('verified fix')
    assert item['summaryOnly'] and item['contentAvailable'] and item['actor']=='agent'
    assert store.db.connection.execute('SELECT storage_zip FROM issues WHERE id=?',(issue['id'],)).fetchone()[0]
    assert 'not inferred' in digest['provenance']


def test_digest_http_bounds_and_provenance(tmp_path):
    """The digest is a fixed read route without a model or arbitrary files."""
    with TestClient(create_app(tmp_path/'http'),base_url='http://127.0.0.1:18995') as client:
        value=client.get('/api/v1/problems/digest?limit=1').json()
        assert value['contract']=='ag-tasks-problems/v1' and value['items']==[]
        assert client.get('/api/v1/problems/digest?limit=101').status_code==422
