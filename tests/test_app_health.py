"""Smoke test: /api/health returns the expected fields."""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


@pytest.mark.skipif(not FASTAPI_AVAILABLE, reason="fastapi not installed")
def test_health_returns_runtime_metadata(tmp_path: Path):
    from praxdaily.app import create_app
    from praxdaily import __version__

    app = create_app(cwd=tmp_path)
    client = TestClient(app)

    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["praxdaily_version"] == __version__
    assert data["cwd"] == str(tmp_path)
    # .prax/ doesn't exist in tmp_path → must be False
    assert data["prax_dir_exists"] is False
    # prax_on_path / prax_version are best-effort; just check the keys exist
    assert "prax_on_path" in data
    assert "prax_version" in data


@pytest.mark.skipif(not FASTAPI_AVAILABLE, reason="fastapi not installed")
def test_index_serves_html(tmp_path: Path):
    from praxdaily.app import create_app

    app = create_app(cwd=tmp_path)
    client = TestClient(app)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "praxdaily" in resp.text.lower()
    assert "/api/health" in resp.text  # the Vue script fetches it
