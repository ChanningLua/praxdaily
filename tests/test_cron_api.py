"""Cron CRUD route tests.

Dispatcher install/uninstall/run-once go through subprocess and are
patched here — we don't actually mutate launchd / crontab from CI.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

try:
    from fastapi.testclient import TestClient

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


pytestmark = pytest.mark.skipif(not FASTAPI_AVAILABLE, reason="fastapi not installed")


def _client(tmp_path: Path) -> TestClient:
    from praxdaily.app import create_app

    return TestClient(create_app(cwd=tmp_path))


def test_list_jobs_empty(tmp_path):
    r = _client(tmp_path).get("/api/cron")
    assert r.status_code == 200
    assert r.json() == {"jobs": []}


def test_upsert_job_writes_yaml(tmp_path):
    client = _client(tmp_path)
    r = client.put(
        "/api/cron/daily-news",
        json={
            "schedule": "0 17 * * *",
            "prompt": "触发 ai-news-daily 技能",
            "notify_on": ["success", "failure"],
            "notify_channel": "my-wechat",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "daily-news"
    assert body["schedule"] == "0 17 * * *"

    yaml_path = tmp_path / ".prax" / "cron.yaml"
    assert yaml_path.exists()
    on_disk = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert on_disk == {
        "jobs": [
            {
                "name": "daily-news",
                "schedule": "0 17 * * *",
                "prompt": "触发 ai-news-daily 技能",
                "notify_on": ["success", "failure"],
                "notify_channel": "my-wechat",
            }
        ]
    }


def test_upsert_replaces_existing_job_with_same_name(tmp_path):
    client = _client(tmp_path)
    client.put(
        "/api/cron/x",
        json={"schedule": "0 1 * * *", "prompt": "v1"},
    )
    client.put(
        "/api/cron/x",
        json={"schedule": "0 2 * * *", "prompt": "v2"},
    )
    jobs = client.get("/api/cron").json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["schedule"] == "0 2 * * *"
    assert jobs[0]["prompt"] == "v2"


def test_upsert_rejects_invalid_notify_trigger(tmp_path):
    r = _client(tmp_path).put(
        "/api/cron/bad",
        json={"schedule": "* * * * *", "prompt": "x", "notify_on": ["meow"]},
    )
    assert r.status_code == 400
    assert "meow" in r.json()["detail"]


def test_upsert_rejects_path_traversal_name(tmp_path):
    r = _client(tmp_path).put(
        "/api/cron/..%2Fevil",
        json={"schedule": "* * * * *", "prompt": "x"},
    )
    assert r.status_code in (400, 404)


def test_delete_job(tmp_path):
    client = _client(tmp_path)
    client.put("/api/cron/x", json={"schedule": "* * * * *", "prompt": "x"})
    r = client.delete("/api/cron/x")
    assert r.status_code == 200
    assert client.get("/api/cron").json() == {"jobs": []}


def test_delete_missing_returns_404(tmp_path):
    r = _client(tmp_path).delete("/api/cron/nope")
    assert r.status_code == 404


def test_run_once_shells_out_to_prax_cron_run(tmp_path):
    client = _client(tmp_path)
    fake_proc = type("P", (), {"returncode": 0, "stdout": "Dispatched 1 due job", "stderr": ""})()
    with patch("praxdaily.routes.cron.shutil.which", return_value="/fake/prax"), \
         patch("praxdaily.routes.cron.subprocess.run", return_value=fake_proc) as mock_run:
        r = client.post("/api/cron/run-once")
    assert r.status_code == 200
    assert "Dispatched" in r.json()["output"]
    args = mock_run.call_args.args[0]
    assert args == ["/fake/prax", "cron", "run"]


def test_install_dispatcher_propagates_failure(tmp_path):
    client = _client(tmp_path)
    fake_proc = type("P", (), {"returncode": 1, "stdout": "", "stderr": "permission denied"})()
    with patch("praxdaily.routes.cron.shutil.which", return_value="/fake/prax"), \
         patch("praxdaily.routes.cron.subprocess.run", return_value=fake_proc):
        r = client.post("/api/cron/install")
    assert r.status_code == 500
    assert "permission denied" in r.json()["detail"]


def test_run_once_503_when_prax_cli_missing(tmp_path):
    client = _client(tmp_path)
    with patch("praxdaily.routes.cron.shutil.which", return_value=None):
        r = client.post("/api/cron/run-once")
    assert r.status_code == 503
    assert "prax CLI not on PATH" in r.json()["detail"]
