"""Workspace registry route + multi-cwd routing tests."""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


pytestmark = pytest.mark.skipif(not FASTAPI_AVAILABLE, reason="fastapi not installed")


def _client(default_cwd: Path) -> TestClient:
    """Spin up an app with default_cwd and a fresh ~/.praxdaily/."""
    from praxdaily.app import create_app

    return TestClient(create_app(cwd=default_cwd))


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Redirect Path.home() so workspaces.json doesn't touch the real home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")


def test_get_workspaces_seeds_with_default(tmp_path):
    proj = tmp_path / "p1"
    proj.mkdir()
    r = _client(proj).get("/api/workspaces")
    data = r.json()
    assert data["current"] == str(proj.resolve())
    assert data["known"] == [str(proj.resolve())]


def test_register_new_workspace_selects_it(tmp_path):
    p1 = tmp_path / "p1"; p1.mkdir()
    p2 = tmp_path / "p2"; p2.mkdir()
    client = _client(p1)
    r = client.post("/api/workspaces", json={"path": str(p2)})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["current"] == str(p2.resolve())
    assert set(data["known"]) == {str(p1.resolve()), str(p2.resolve())}


def test_register_rejects_nonexistent_path(tmp_path):
    p1 = tmp_path / "p1"; p1.mkdir()
    r = _client(p1).post("/api/workspaces", json={"path": "/no/such/dir/anywhere"})
    assert r.status_code == 400
    assert "does not exist" in r.json()["detail"]


def test_register_rejects_relative_path(tmp_path):
    p1 = tmp_path / "p1"; p1.mkdir()
    r = _client(p1).post("/api/workspaces", json={"path": "../somewhere"})
    assert r.status_code == 400
    assert "absolute" in r.json()["detail"]


def test_register_rejects_protected_system_path(tmp_path):
    p1 = tmp_path / "p1"; p1.mkdir()
    r = _client(p1).post("/api/workspaces", json={"path": "/System/Library"})
    assert r.status_code == 400
    assert "protected" in r.json()["detail"]


def test_register_rejects_root(tmp_path):
    p1 = tmp_path / "p1"; p1.mkdir()
    r = _client(p1).post("/api/workspaces", json={"path": "/"})
    assert r.status_code == 400


def test_select_requires_known_path(tmp_path):
    p1 = tmp_path / "p1"; p1.mkdir()
    p2 = tmp_path / "p2"; p2.mkdir()
    client = _client(p1)
    r = client.post("/api/workspaces/select", json={"path": str(p2)})
    assert r.status_code == 400
    assert "not registered" in r.json()["detail"]


def test_select_switches_active_workspace(tmp_path):
    p1 = tmp_path / "p1"; p1.mkdir()
    p2 = tmp_path / "p2"; p2.mkdir()
    client = _client(p1)
    client.post("/api/workspaces", json={"path": str(p2)})
    # add p2, current is now p2; switch back to p1
    r = client.post("/api/workspaces/select", json={"path": str(p1.resolve())})
    assert r.status_code == 200
    assert r.json()["current"] == str(p1.resolve())


def test_remove_workspace_falls_back_to_other(tmp_path):
    p1 = tmp_path / "p1"; p1.mkdir()
    p2 = tmp_path / "p2"; p2.mkdir()
    client = _client(p1)
    client.post("/api/workspaces", json={"path": str(p2)})
    r = client.delete(f"/api/workspaces?path={p2.resolve()}")
    data = r.json()
    assert data["current"] == str(p1.resolve())
    assert str(p2.resolve()) not in data["known"]


def test_health_reports_active_workspace(tmp_path):
    """Health's `cwd` field must reflect the selected workspace, not the
    server-launch default — that's how the GUI knows which workspace it
    is editing."""
    p1 = tmp_path / "p1"; p1.mkdir()
    p2 = tmp_path / "p2"; p2.mkdir()
    client = _client(p1)
    client.post("/api/workspaces", json={"path": str(p2)})
    h = client.get("/api/health").json()
    assert h["cwd"] == str(p2.resolve())
    assert h["default_cwd"] == str(p1)


def test_other_routes_follow_active_workspace(tmp_path):
    """Critical: when the user switches workspace, channels/cron/sources
    yaml IO must move with it. Verify by writing a channel under p2 and
    checking it doesn't leak into p1."""
    p1 = tmp_path / "p1"; p1.mkdir()
    p2 = tmp_path / "p2"; p2.mkdir()
    client = _client(p1)
    client.post("/api/workspaces", json={"path": str(p2)})

    # Write a channel — should land in p2/.prax/notify.yaml
    client.put(
        "/api/channels/c1",
        json={"provider": "feishu_webhook", "url": "https://x"},
    )
    assert (p2 / ".prax" / "notify.yaml").exists()
    assert not (p1 / ".prax" / "notify.yaml").exists()

    # Switch back to p1, list channels — should be empty (no notify.yaml).
    client.post("/api/workspaces/select", json={"path": str(p1.resolve())})
    assert client.get("/api/channels").json() == {"channels": []}

    # Switch to p2, channel reappears.
    client.post("/api/workspaces/select", json={"path": str(p2.resolve())})
    assert len(client.get("/api/channels").json()["channels"]) == 1
