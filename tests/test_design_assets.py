"""Serve shared design assets locally without collection, external requests or working user data."""

import pytest
from fastapi.testclient import TestClient
from ag_tasks.app import create_app


@pytest.mark.parametrize("asset", ["/", "/design-adapter.css", "/vendor/ag-design/index.js", "/vendor/ag-design/tokens.css", "/vendor/ag-design/styles.css", "/vendor/ag-design/audit.js", "/vendor/ag-design/manifest.json"])
def test_shared_design_assets_are_local_and_csp_preserved(tmp_path, asset):
    """An isolated app serves every packaged design asset under its unchanged local HTTP policy."""
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8898") as client:
        response = client.get(asset)
        assert response.status_code == 200
        assert response.content
        assert "unsafe-eval" not in response.headers["content-security-policy"]
        if asset == "/":
            assert "/vendor/ag-design/tokens.css" in response.text
            assert "/design-adapter.css" in response.text
