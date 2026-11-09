"""Bounded operation metadata proves request/response status without capturing issue bodies."""
from fastapi.testclient import TestClient
from ag_tasks.app import create_app


def test_widget_records_real_routes_and_status_without_issue_bodies_or_polling(tmp_path):
    """Unknown clients stay unknown; no source calls, actor inference or persisted metadata journal."""
    with TestClient(create_app(tmp_path),base_url='http://127.0.0.1:8895') as client:
        assert client.get('/api/desktop/v1/widget').json()['requests']==[]
        for _ in range(2):assert client.get('/api/desktop/v1/widget').json()['requests']==[]
        result=client.post('/api/v1/agent/issues',json={'title':'PRIVATE TITLE','description':'password=PRIVATE SECRET'})
        assert result.status_code==201
        row=client.get('/api/desktop/v1/widget').json()['requests'][0]
        assert row['operation']=='POST /api/v1/agent/issues' and row['status']=='succeeded'
        assert row['responseSummary']=='HTTP 201' and row['durationMs']>=0
        assert row['actorLabel']=='Agent API client (unidentified)'
        for _ in range(40):assert client.get('/api/v1/issues').status_code==200
        response=client.get('/api/desktop/v1/widget');value=response.json()
        assert value['schemaVersion']=='ag-desktop/widget-v1' and value['appId']=='ag-tasks' and len(value['requests'])==8
        assert 'PRIVATE' not in response.text and 'password' not in response.text
        assert all(len(text)<=512 for row in value['requests'] for text in row.values() if isinstance(text,str))
        assert response.headers['cache-control']=='no-store'
        assert client.get('/api/desktop/v1/widget',headers={'Origin':'https://other.invalid'}).status_code==403
        assert client.get('/api/desktop/v1/widget',headers={'Host':'other.invalid'}).status_code==403
        assert client.get('/desktop-activity.html').status_code==200


def test_new_process_has_empty_metadata_history(tmp_path):
    """No historical queue is silently read from issue content after a restart."""
    with TestClient(create_app(tmp_path),base_url='http://127.0.0.1:8895') as client:
        client.get('/api/v1/issues');assert client.get('/api/desktop/v1/widget').json()['requests']
    with TestClient(create_app(tmp_path),base_url='http://127.0.0.1:8895') as client:
        assert client.get('/api/desktop/v1/widget').json()['requests']==[]


def test_unicode_snapshot_budget_keeps_newest_actual_rows():
    """Worst-case512-scalar fields fit the60KiB wire budget without inventing or truncating a row."""
    import json
    from ag_tasks.desktop_widget import bounded_snapshot
    rows = [{'operation': str(index), **{key: '😀' * 512 for key in ('timestamp', 'actorLabel', 'requestSummary', 'responseSummary')}} for index in range(8)]
    result = bounded_snapshot({'requests': rows, 'source': 'Actual retained rows'})
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) <= 60 * 1024
    assert 0 < len(result['requests']) < 8
    assert [row['operation'] for row in result['requests']] == [str(index) for index in range(len(result['requests']))]
    assert 'omitted' in result['source']
