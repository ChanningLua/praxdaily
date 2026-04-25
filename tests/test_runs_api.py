"""Runs (cron log) route tests."""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


pytestmark = pytest.mark.skipif(not FASTAPI_AVAILABLE, reason="fastapi not installed")


def _client(tmp_path: Path) -> TestClient:
    from praxdaily.app import create_app

    return TestClient(create_app(cwd=tmp_path))


def _make_log(tmp_path: Path, filename: str, body: str) -> Path:
    logs = tmp_path / ".prax" / "logs" / "cron"
    logs.mkdir(parents=True, exist_ok=True)
    p = logs / filename
    p.write_text(body, encoding="utf-8")
    return p


def test_list_runs_empty(tmp_path):
    r = _client(tmp_path).get("/api/runs")
    assert r.status_code == 200
    assert r.json() == {"runs": []}


def test_list_runs_returns_newest_first(tmp_path):
    _make_log(tmp_path, "alpha-20260101-120000.log", "old run")
    _make_log(tmp_path, "alpha-20260425-150000.log", "new run")
    _make_log(tmp_path, "beta-20260301-100000.log", "middle run")
    runs = _client(tmp_path).get("/api/runs").json()["runs"]
    # Sorted by filename descending — newest timestamp first.
    assert runs[0]["filename"] == "alpha-20260425-150000.log"
    assert runs[-1]["filename"] == "alpha-20260101-120000.log"
    # Each entry carries the parsed name + ISO timestamp.
    assert runs[0]["name"] == "alpha"
    assert runs[0]["started_at"].startswith("2026-04-25T15:00:00")


def test_list_runs_skips_unparseable_filenames(tmp_path):
    _make_log(tmp_path, "garbage.log", "x")
    _make_log(tmp_path, "alpha-20260425-150000.log", "ok")
    runs = _client(tmp_path).get("/api/runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["name"] == "alpha"


def test_get_run_returns_content_and_inferred_status_success(tmp_path):
    _make_log(
        tmp_path,
        "alpha-20260425-150000.log",
        "$ prax prompt 'hi'\n\n[prax] model=gpt-5.4\n你好。",
    )
    r = _client(tmp_path).get("/api/runs/alpha-20260425-150000.log")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "success"
    assert "你好" in data["content"]


def test_get_run_infers_failure_from_log_markers(tmp_path):
    _make_log(
        tmp_path,
        "alpha-20260425-150000.log",
        "[prax] llm_call failure 1/3: ConnectError\n",
    )
    data = _client(tmp_path).get("/api/runs/alpha-20260425-150000.log").json()
    assert data["status"] == "failure"


def test_get_run_404_when_missing(tmp_path):
    r = _client(tmp_path).get("/api/runs/nope-20260425-150000.log")
    assert r.status_code == 404


def test_get_run_blocks_path_traversal(tmp_path):
    # Drop a sensitive file outside logs dir.
    secret = tmp_path / "secret.txt"
    secret.write_text("nuclear codes")

    # FastAPI's URL parser treats "%2F" as a literal slash inside a path
    # segment, so the traversal payload doesn't even reach our endpoint —
    # the URL no longer matches /api/runs/{filename}. We accept either
    # 400 (our explicit check) or 404 (FastAPI routing) as "blocked".
    r = _client(tmp_path).get("/api/runs/..%2F..%2Fsecret.txt")
    assert r.status_code in (400, 404)
    # Either way the response must NOT leak the secret content.
    assert "nuclear codes" not in r.text


def test_get_run_rejects_dotdot_prefix(tmp_path):
    r = _client(tmp_path).get("/api/runs/..hidden.log")
    assert r.status_code == 400
