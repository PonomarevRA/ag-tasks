"""Project routing and organizational migration use isolated data and real service commands."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from ag_tasks.app import create_app
from ag_tasks.issues import DomainError, IssueStore
from test_service_data import command
from workflow_helpers import metadata, review_ready


@pytest.fixture
def store(tmp_path):
    """
    Own an isolated task store without opening live user data.
    """
    value = IssueStore(tmp_path)
    yield value
    value.close()


def ensure(store, key='test-project', prefix=None, request_key=None):
    """
    Ensure an explicit fixture project through the production command journal.
    """
    payload = {'projectKey':key,'name':key}
    if prefix:
        payload['prefix'] = prefix
    return command(store,'project.ensure',payload,key=request_key)


def tree(store):
    """
    Create one plan with a completed reviewed child and another live work session.
    """
    root = store.create({'title':'Root'})
    complete = store.create({'title':'Reviewed child','parentId':root['id'],**metadata()})
    review_ready(store,complete['id'])
    live = store.create({'title':'Live child','parentId':root['id'],**metadata()})
    store.execution.start(live['id'],{'agentId':'live-worker','sessionKey':'live-key'})
    ids = [row[0] for row in store.db.connection.execute('SELECT id FROM issues')]
    return root, ids, live


def move(store, root, ids, project='test-project', request_key=None):
    """
    Move an exact source tree through its journaled public operation.
    """
    return command(store,'project.move_tree',{'rootId':root['id'],'expectedSpaceId':'default',
        'expectedIds':ids,'projectKey':project},key=request_key)


def test_concurrent_ensure_and_retry_preserve_one_identity(store):
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _:ensure(store,request_key='same'),range(12)))
    assert len({row['id'] for row in results})==1
    assert len(store.spaces.list())==2
    original=results[0]
    again=command(store,'project.ensure',{'projectKey':'TEST-PROJECT','name':'Changed suggestion','prefix':'OTHER'})
    assert again['id']==original['id'] and again['name']==original['name'] and again['prefix']==original['prefix']
    assert command(store,'project.ensure',{'projectKey':'test-project','name':'test-project'},key='same')==original


def test_existing_named_space_is_bound_but_default_and_other_owners_are_rejected(store):
    existing=store.spaces.write({'name':'Project','prefix':'PROJ'})
    bound=command(store,'project.ensure',{'projectKey':'project','name':'Project'})
    assert bound['id']==existing['id'] and not bound['created']
    for key,name in [('another','Project'),('default-project','Default')]:
        with pytest.raises(DomainError) as error:
            command(store,'project.ensure',{'projectKey':key,'name':name})
        assert error.value.code=='project_space_conflict'


def test_project_roots_never_fall_back_but_children_inherit_exact_owner(store):
    with pytest.raises(DomainError) as error:
        store.create({'title':'Forgot project','codeTask':True})
    assert error.value.code=='project_required'
    assert len(store.spaces.list())==1 and store.list()==[]
    plan=store.planning.create_plan({'title':'Plan','markdown':'# Plan','codeTask':True,
        'projectKey':'fixture','projectName':'Fixture','projectPrefix':'FIX'})
    child=store.create({'title':'Child','parentId':plan['id'],'codeTask':True})
    assert plan['spaceId']==child['spaceId'] and plan['spaceId']!='default'
    assert plan['key']=='FIX-1'
    with pytest.raises(DomainError):
        store.create({'title':'Wrong space','projectKey':'fixture','spaceId':'default'})
    with pytest.raises(DomainError):
        store.create({'title':'Wrong child','parentId':plan['id'],'projectKey':'different'})
    assert len(store.spaces.list())==2  # Failed child routing rolls back its tentative space too.


def test_move_preserves_results_reviews_leases_links_and_every_old_key(store):
    root,ids,live=tree(store)
    external=store.create({'title':'External'})
    store.link(external['id'],live['id'],'related')
    project=ensure(store,prefix='PRJ')
    before={table:[tuple(row) for row in store.db.connection.execute('SELECT * FROM '+table)]
        for table in ('results','review_reports','work_sessions','issue_links')}
    old={row['id']:store.get(row['id']) for row in store.db.connection.execute('SELECT id FROM issues')}
    moved=move(store,root,ids,request_key='migration')
    assert moved['moved']==len(ids)
    assert move(store,root,ids,request_key='migration')==moved
    for table,rows in before.items():
        assert [tuple(row) for row in store.db.connection.execute('SELECT * FROM '+table)]==rows
    for item in moved['items']:
        current=store.get(item['oldKey'])
        assert current['id']==item['id'] and current['spaceId']==project['id']
        for field in ('status','parentId','definitionRevision','resultGeneration','currentResult','criticReview','lastActivityAt'):
            assert current[field]==old[item['id']][field]
        assert store.get(item['newKey'])['id']==item['id']
    assert store.db.connection.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_membership_conflict_and_sql_failure_never_partially_move(store):
    root,ids,_=tree(store);ensure(store)
    before=[tuple(row) for row in store.db.connection.execute('SELECT id,space_id,space_number FROM issues')]
    with pytest.raises(DomainError) as error:
        move(store,root,ids[:-1])
    assert error.value.code=='tree_changed'
    store.db.connection.execute("CREATE TEMP TRIGGER stop_migration BEFORE UPDATE OF space_id ON issues BEGIN SELECT RAISE(ABORT,'fault'); END")
    with pytest.raises(sqlite3.Error):
        move(store,root,ids)
    assert [tuple(row) for row in store.db.connection.execute('SELECT id,space_id,space_number FROM issues')]==before
    assert store.db.connection.execute("SELECT COUNT(*) FROM history WHERE action='project_space_moved'").fetchone()[0]==0


def test_cross_space_dependency_still_blocks_and_rejects_cycles_after_move(store):
    one=store.create({'title':'One',**metadata()});two=store.create({'title':'Two',**metadata()})
    store.link(one['id'],two['id'],'blocks')
    ensure(store,'one');ensure(store,'two')
    move(store,one,[one['id']],project='one');move(store,two,[two['id']],project='two')
    with pytest.raises(DomainError) as error:
        store.execution.start(two['id'],{'agentId':'worker','sessionKey':'blocked'})
    assert error.value.code=='issue_blocked'
    with pytest.raises(DomainError) as error:
        store.link(two['id'],one['id'],'blocks')
    assert error.value.code=='dependency_cycle'
    assert store.get(one['key'])['id']==one['id']


def test_http_project_contract_and_code_plan_admission(tmp_path):
    with TestClient(create_app(tmp_path),base_url='http://127.0.0.1:8895') as client:
        def submit(operation,payload,key):
            return client.post('/api/v1/agent/commands',json={'clientId':'fixture','commandId':key,
                'operation':operation,'payload':payload})
        result=submit('project.ensure',{'projectKey':'app','name':'Application','prefix':'APP'},'ensure')
        assert result.status_code==200
        space=result.json()
        assert next(row for row in client.get('/api/v1/spaces').json()['items'] if row['id']==space['id'])['projectKey']=='app'
        missing=client.post('/api/v1/agent/plans',json={'title':'Missing','markdown':'# Plan','codeTask':True})
        assert missing.status_code==409 and missing.json()['error']['code']=='project_required'
        plan=client.post('/api/v1/agent/plans',json={'title':'Owned','markdown':'# Plan','projectKey':'app','codeTask':True})
        assert plan.status_code==201 and plan.json()['spaceId']==space['id']


def test_migration_preserves_cold_payload_representation_without_restoring_it(store):
    from datetime import datetime,timedelta,timezone
    root=store.create({'title':'Cold plan','description':'Archived content'})
    assert store.cold.sweep(now=datetime.now(timezone.utc)+timedelta(days=8))==[root['id']]
    before=tuple(store.db.connection.execute('SELECT storage_zip,storage_sha256,description FROM issues WHERE id=?',(root['id'],)).fetchone())
    assert before[0] and before[2]==''
    assert store.service.project_migration.inspect(root['key'])['expectedIds']==[root['id']]
    ensure(store)
    move(store,root,[root['id']])
    assert tuple(store.db.connection.execute('SELECT storage_zip,storage_sha256,description FROM issues WHERE id=?',(root['id'],)).fetchone())==before
    assert store.db.connection.execute("SELECT COUNT(*) FROM history WHERE action='storage_restored'").fetchone()[0]==0
