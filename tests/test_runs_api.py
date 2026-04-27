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
    assert r.json() == {"runs": [], "total": 0, "filtered_total": 0}


def test_list_runs_returns_newest_first(tmp_path):
    _make_log(tmp_path, "alpha-20260101-120000.log", "old run")
    _make_log(tmp_path, "alpha-20260425-150000.log", "new run")
    _make_log(tmp_path, "beta-20260301-100000.log", "middle run")
    data = _client(tmp_path).get("/api/runs").json()
    runs = data["runs"]
    # Sorted by filename descending — newest timestamp first.
    assert runs[0]["filename"] == "alpha-20260425-150000.log"
    assert runs[-1]["filename"] == "alpha-20260101-120000.log"
    # Each entry carries the parsed name + ISO timestamp.
    assert runs[0]["name"] == "alpha"
    assert runs[0]["started_at"].startswith("2026-04-25T15:00:00")
    assert data["total"] == 3
    assert data["filtered_total"] == 3


def test_list_runs_skips_unparseable_filenames(tmp_path):
    _make_log(tmp_path, "garbage.log", "x")
    _make_log(tmp_path, "alpha-20260425-150000.log", "ok")
    data = _client(tmp_path).get("/api/runs").json()
    assert len(data["runs"]) == 1
    assert data["runs"][0]["name"] == "alpha"
    assert data["total"] == 1


# ── search / filter ─────────────────────────────────────────────────────────


def test_filter_by_name_substring(tmp_path):
    _make_log(tmp_path, "ai-news-20260425-150000.log", "ok")
    _make_log(tmp_path, "ai-news-20260425-160000.log", "ok")
    _make_log(tmp_path, "code-review-20260425-170000.log", "ok")
    _make_log(tmp_path, "deploy-20260425-180000.log", "ok")

    data = _client(tmp_path).get("/api/runs?name=news").json()
    assert data["total"] == 4
    assert data["filtered_total"] == 2
    assert all("news" in r["name"] for r in data["runs"])


def test_filter_by_name_is_case_insensitive(tmp_path):
    _make_log(tmp_path, "Daily-News-20260425-150000.log", "ok")
    data = _client(tmp_path).get("/api/runs?name=DAILY").json()
    assert data["filtered_total"] == 1


def test_filter_by_status(tmp_path):
    _make_log(tmp_path, "alpha-20260425-150000.log", "[prax] model=gpt\n你好")
    _make_log(tmp_path, "alpha-20260425-160000.log", "[prax] llm_call failure 1/3: ConnectError")
    _make_log(tmp_path, "alpha-20260425-170000.log", "Traceback (most recent call last):\n  File ...")

    success = _client(tmp_path).get("/api/runs?status=success").json()
    assert success["filtered_total"] == 1

    failure = _client(tmp_path).get("/api/runs?status=failure").json()
    assert failure["filtered_total"] == 2


def test_filter_combined_name_and_status(tmp_path):
    _make_log(tmp_path, "ai-news-20260425-150000.log", "[prax] llm_call failure")
    _make_log(tmp_path, "ai-news-20260425-160000.log", "[prax] model=gpt")
    _make_log(tmp_path, "deploy-20260425-170000.log", "[prax] llm_call failure")

    data = _client(tmp_path).get("/api/runs?name=news&status=failure").json()
    assert data["total"] == 3
    assert data["filtered_total"] == 1
    assert data["runs"][0]["filename"].startswith("ai-news-20260425-150000")


def test_invalid_status_returns_400(tmp_path):
    r = _client(tmp_path).get("/api/runs?status=garbage")
    assert r.status_code == 400
    assert "invalid status filter" in r.json()["detail"]


def test_limit_caps_results(tmp_path):
    for i in range(15):
        _make_log(tmp_path, f"job-2026042{5}-1500{i:02d}.log", "ok")
    data = _client(tmp_path).get("/api/runs?limit=5").json()
    assert data["total"] == 15
    assert data["filtered_total"] == 15
    assert len(data["runs"]) == 5
    # Still newest-first within the cap.
    assert data["runs"][0]["filename"].endswith("150014.log")


def test_limit_clamps_to_1000(tmp_path):
    """Defensive: even if a caller asks for limit=999999 we don't allocate
    a giant list."""
    _make_log(tmp_path, "x-20260425-150000.log", "ok")
    data = _client(tmp_path).get("/api/runs?limit=999999").json()
    assert len(data["runs"]) == 1  # only one log existed


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


# ── 0.7.1: latest-digest preview ───────────────────────────────────────────


def _make_digest(tmp_path: Path, date: str, content: str) -> Path:
    vault = tmp_path / ".prax" / "vault" / date
    vault.mkdir(parents=True, exist_ok=True)
    p = vault / "daily-digest.md"
    p.write_text(content, encoding="utf-8")
    return p


def test_latest_digest_returns_present_false_when_no_vault(tmp_path):
    r = _client(tmp_path).get("/api/runs/latest-digest")
    assert r.status_code == 200
    body = r.json()
    assert body["present"] is False
    assert "no vault" in body["reason"]


def test_latest_digest_returns_today_when_one_run(tmp_path):
    _make_digest(tmp_path, "2026-04-27", "# AI 日报\n\ntoday's content")
    r = _client(tmp_path).get("/api/runs/latest-digest")
    body = r.json()
    assert body["present"] is True
    assert body["date"] == "2026-04-27"
    assert "today's content" in body["content"]
    assert body["chars"] == len(body["content"])


def test_latest_digest_picks_newest_when_multiple_dates(tmp_path):
    """Sort by ISO date string (works as wall-clock for YYYY-MM-DD)."""
    _make_digest(tmp_path, "2026-04-25", "old")
    _make_digest(tmp_path, "2026-04-27", "newest")
    _make_digest(tmp_path, "2026-04-26", "middle")
    body = _client(tmp_path).get("/api/runs/latest-digest").json()
    assert body["date"] == "2026-04-27"
    assert "newest" in body["content"]


def test_latest_digest_skips_empty_date_dirs(tmp_path):
    """If a date folder exists but no daily-digest.md inside (run failed
    before write), fall through to the next-newest with content."""
    (tmp_path / ".prax" / "vault" / "2026-04-27").mkdir(parents=True)  # empty
    _make_digest(tmp_path, "2026-04-26", "yesterday's content")
    body = _client(tmp_path).get("/api/runs/latest-digest").json()
    assert body["present"] is True
    assert body["date"] == "2026-04-26"


def test_latest_digest_does_not_collide_with_filename_route(tmp_path):
    """Regression guard: the /{filename} route once swallowed
    /latest-digest as a filename. Both should work independently."""
    _make_log(tmp_path, "job-20260427-140000.log", "log body")
    _make_digest(tmp_path, "2026-04-27", "digest body")

    r1 = _client(tmp_path).get("/api/runs/latest-digest")
    r2 = _client(tmp_path).get("/api/runs/job-20260427-140000.log")
    assert r1.status_code == 200 and "digest body" in r1.json()["content"]
    assert r2.status_code == 200 and "log body" in r2.json()["content"]
