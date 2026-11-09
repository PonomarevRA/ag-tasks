"""Malformed agent fields must fail safely without creating tasks or escalating to storage errors."""

import pytest
from fastapi.testclient import TestClient
from ag_tasks.app import create_app


@pytest.mark.parametrize('patch', [{'parentId': []}, {'repoId': {}}, {'title': None}, {'status': ['done']}, {'externalRefs': [{'url':'https://user:password@example.com'}]}])
def test_invalid_issue_fields_return_domain_400(tmp_path, patch):
    """Reject invalid typed issue input before SQLite or Git sees it."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        response = client.post('/api/v1/agent/issues',json={'title':'Malformed',**patch})
        assert response.status_code == 400, response.text
        assert client.get('/api/v1/issues').json()['items'] == []
