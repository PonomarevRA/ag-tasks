"""Check that the board uses the same task facts as the agent list."""

from fastapi.testclient import TestClient

from ag_tasks.app import create_app
from workflow_helpers import metadata, review_ready


def test_board_projection_matches_current_evidence_and_pagination(tmp_path):
    """Cover reviewed, working and ordinary tasks through the actual HTTP routes."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        store = client.app.state.store
        parent = store.create({'title': 'Parent', **metadata()})
        child = store.create({'title': 'Reviewed child', 'parentId': parent['id'], **metadata()})
        review_ready(store, child['id'])
        active = store.create({'title': 'Active', **metadata()})
        store.execution.start(active['id'], {'agentId': 'fixture', 'sessionKey': 'board-test'})
        path = '/api/v1/issues?spaceId=default&limit=2'
        original = client.get(path).json()
        board = client.get(path + '&board=true').json()
        assert board['hasMore'] is True and board['nextOffset'] == 2
        assert original['nextOffset'] == board['nextOffset']
        for expected, item in zip(original['items'], board['items'], strict=True):
            assert item['id'] == expected['id']
            assert item['lifecycle'] == expected['lifecycle']
            assert item['work']['state'] == expected['work']['state']
            assert 'description' in expected and 'description' not in item
            assert 'currentResult' not in item and 'activeSession' not in item['work']
        tail = client.get(path + '&board=true&offset=2').json()
        assert not tail['hasMore'] and tail['nextOffset'] is None
        assert [row['id'] for row in tail['items']] == [child['id'], parent['id']]
        assert client.get('/api/v1/issues/' + child['id']).json()['criticReview']['verdict'] == 'pass'
        assert client.get(path + '&board=true&offset=9223372036854775808').status_code == 400
