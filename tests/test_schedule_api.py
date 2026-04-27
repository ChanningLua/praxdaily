"""HTTP route layer for schedule management. Just verifies the routes
hand off correctly to scheduler.py — the underlying behaviour is
covered by test_scheduler."""

from __future__ import annotations

import platform
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


pytestmark = [
    pytest.mark.skipif(not FASTAPI_AVAILABLE, reason="fastapi not installed"),
    pytest.mark.skipif(platform.system() != "Darwin", reason="scheduler is macOS-only for now"),
]


def _client(cwd: Path) -> TestClient:
    from praxdaily.app import create_app
    return TestClient(create_app(cwd=cwd))


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")


@pytest.fixture(autouse=True)
def _stub_launchctl(monkeypatch):
    """Don't actually call launchctl in tests."""
    from praxdaily import scheduler

    class _Result:
        returncode = 0; stderr = ""; stdout = ""
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **kw: _Result())


def test_get_schedule_when_nothing_installed(tmp_path):
    r = _client(tmp_path).get("/api/schedule")
    body = r.json()
    assert body["praxdaily"]["installed"] is False
    assert body["legacy_prax_cron"]["present"] is False


def test_install_creates_plist(tmp_path):
    client = _client(tmp_path)
    r = client.post("/api/schedule/install", json={"time": "09:30"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["installed"] is True
    assert body["schedule"] == "09:30"
    assert Path(body["plist_path"]).exists()

    # GET reflects the new state
    after = client.get("/api/schedule").json()
    assert after["praxdaily"]["installed"] is True
    assert after["praxdaily"]["schedule"] == "09:30"


def test_install_rejects_bad_time(tmp_path):
    r = _client(tmp_path).post("/api/schedule/install", json={"time": "25:00"})
    assert r.status_code == 400
    assert "hour" in r.json()["detail"]


def test_install_default_time_is_14_00(tmp_path):
    r = _client(tmp_path).post("/api/schedule/install", json={})
    assert r.json()["schedule"] == "14:00"


def test_uninstall_removes_plist(tmp_path):
    client = _client(tmp_path)
    client.post("/api/schedule/install", json={"time": "10:00"})
    r = client.delete("/api/schedule")
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert client.get("/api/schedule").json()["praxdaily"]["installed"] is False


def test_get_surfaces_legacy_prax_cron_when_present(tmp_path):
    legacy = tmp_path / "fake-home" / "Library" / "LaunchAgents" / "dev.prax.cron.dispatcher.plist"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("<plist></plist>", encoding="utf-8")

    body = _client(tmp_path).get("/api/schedule").json()
    assert body["legacy_prax_cron"]["present"] is True


def test_uninstall_legacy_endpoint_removes_old_plist(tmp_path):
    legacy = tmp_path / "fake-home" / "Library" / "LaunchAgents" / "dev.prax.cron.dispatcher.plist"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("<plist></plist>", encoding="utf-8")

    r = _client(tmp_path).post("/api/schedule/uninstall-legacy-prax-cron")
    assert r.json()["removed"] is True
    assert not legacy.exists()
