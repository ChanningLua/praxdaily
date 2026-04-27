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
    # Channel must exist before cron job can reference it (post-0.4.1).
    client.put(
        "/api/channels/my-wechat",
        json={
            "provider": "wechat_personal",
            "account_id": "ilink_xxx@im.bot",
        },
    )
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


# ── 0.4.1: notify_channel must exist + per-job trigger-now ────────────────


def test_upsert_rejects_notify_channel_that_doesnt_exist(tmp_path):
    """Saving a cron job that points at a missing channel should 400 —
    otherwise the job runs and silently no-ops the notify step at runtime."""
    r = _client(tmp_path).put(
        "/api/cron/orphan",
        json={
            "schedule": "0 17 * * *",
            "prompt": "x",
            "notify_on": ["success"],
            "notify_channel": "nonexistent-channel",
        },
    )
    assert r.status_code == 400
    assert "nonexistent-channel" in r.json()["detail"]


def test_upsert_accepts_when_no_notify_channel_specified(tmp_path):
    """Empty notify_channel must still be accepted — it just means
    'don't send a notification on success/failure'."""
    r = _client(tmp_path).put(
        "/api/cron/silent-job",
        json={"schedule": "0 17 * * *", "prompt": "x"},
    )
    assert r.status_code == 200, r.text


def test_trigger_now_404_on_missing_job(tmp_path):
    r = _client(tmp_path).post("/api/cron/nope/trigger-now")
    assert r.status_code == 404


def test_trigger_now_503_when_prax_cli_missing(tmp_path):
    client = _client(tmp_path)
    client.put("/api/cron/x", json={"schedule": "0 17 * * *", "prompt": "say hi"})
    with patch("praxdaily.routes.cron.shutil.which", return_value=None):
        r = client.post("/api/cron/x/trigger-now")
    assert r.status_code == 503


def test_trigger_now_shells_out_to_prax_prompt_with_job_prompt(tmp_path):
    client = _client(tmp_path)
    client.put(
        "/api/cron/say-hi",
        json={"schedule": "0 17 * * *", "prompt": "用一句话问候我"},
    )
    fake_proc = type(
        "P", (), {"returncode": 0, "stdout": "你好。", "stderr": ""}
    )()
    with patch("praxdaily.routes.cron.shutil.which", return_value="/fake/prax"), \
         patch("praxdaily.routes.cron.subprocess.run", return_value=fake_proc) as mock_run:
        r = client.post("/api/cron/say-hi/trigger-now")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["triggered"] is True
    assert body["exit_code"] == 0
    assert "你好" in body["output_tail"]
    # Critical: the spawned argv must use the job's prompt verbatim, not the
    # job name — bug we'd hit if we shelled to `prax cron run <name>` or
    # similar.
    args = mock_run.call_args.args[0]
    assert args[:3] == ["/fake/prax", "prompt", "用一句话问候我"]
    assert "--permission-mode" in args
    assert "danger-full-access" in args


# ── 0.7: trigger-now must also fire notify so users can verify the chain ─────


def _setup_job_with_notify(client, tmp_path, *, notify_on, notify_channel):
    """Helper: create channel `wechat-self` + job wired to it."""
    client.put(
        f"/api/channels/{notify_channel}",
        json={"provider": "feishu_webhook", "url": "https://example.com/hook"},
    )
    client.put(
        "/api/cron/news",
        json={
            "schedule": "0 17 * * *",
            "prompt": "fetch ai news",
            "notify_on": notify_on,
            "notify_channel": notify_channel,
        },
    )


def test_trigger_now_sends_notify_on_success(tmp_path):
    client = _client(tmp_path)
    _setup_job_with_notify(client, tmp_path, notify_on=["success", "failure"], notify_channel="wechat-self")

    fake_proc = type("P", (), {"returncode": 0, "stdout": "done", "stderr": ""})()
    sent = {}

    class _StubProvider:
        async def send(self, *, title, body, level):
            sent["title"] = title; sent["body"] = body; sent["level"] = level

    with patch("praxdaily.routes.cron.shutil.which", return_value="/fake/prax"), \
         patch("praxdaily.routes.cron.subprocess.run", return_value=fake_proc), \
         patch("prax.tools.notify.build_provider", return_value=_StubProvider()):
        r = client.post("/api/cron/news/trigger-now")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["notify"]["sent"] is True
    assert body["notify"]["channel"] == "wechat-self"
    assert body["notify"]["outcome"] == "success"
    # The provider got called with a useful title/body the user can read.
    assert "✓" in sent["title"] and "news" in sent["title"]
    assert sent["level"] == "info"


def test_trigger_now_sends_notify_on_failure(tmp_path):
    client = _client(tmp_path)
    _setup_job_with_notify(client, tmp_path, notify_on=["failure"], notify_channel="wechat-self")

    fake_proc = type("P", (), {"returncode": 1, "stdout": "", "stderr": "401 Unauthorized"})()

    class _StubProvider:
        sent_level = None
        async def send(self, *, title, body, level):
            type(self).sent_level = level

    with patch("praxdaily.routes.cron.shutil.which", return_value="/fake/prax"), \
         patch("praxdaily.routes.cron.subprocess.run", return_value=fake_proc), \
         patch("prax.tools.notify.build_provider", return_value=_StubProvider()):
        r = client.post("/api/cron/news/trigger-now")
    assert r.status_code == 200
    assert r.json()["notify"]["sent"] is True
    assert _StubProvider.sent_level == "error"


def test_trigger_now_skips_notify_when_outcome_not_in_notify_on(tmp_path):
    """If notify_on=[failure] only and the run succeeds, must NOT spam."""
    client = _client(tmp_path)
    _setup_job_with_notify(client, tmp_path, notify_on=["failure"], notify_channel="wechat-self")

    fake_proc = type("P", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
    with patch("praxdaily.routes.cron.shutil.which", return_value="/fake/prax"), \
         patch("praxdaily.routes.cron.subprocess.run", return_value=fake_proc):
        r = client.post("/api/cron/news/trigger-now")
    body = r.json()
    assert body["notify"]["sent"] is False
    assert "not in notify_on" in body["notify"]["reason"]


def test_trigger_now_skips_notify_when_no_channel_configured(tmp_path):
    client = _client(tmp_path)
    client.put(
        "/api/cron/lone",
        json={"schedule": "0 17 * * *", "prompt": "x"},
    )
    fake_proc = type("P", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
    with patch("praxdaily.routes.cron.shutil.which", return_value="/fake/prax"), \
         patch("praxdaily.routes.cron.subprocess.run", return_value=fake_proc):
        r = client.post("/api/cron/lone/trigger-now")
    assert r.json()["notify"]["sent"] is False


def test_trigger_now_notify_failure_does_not_500(tmp_path):
    """If notify itself blows up, we still return 200 with the prompt result —
    user should see that the run worked even if push failed."""
    client = _client(tmp_path)
    _setup_job_with_notify(client, tmp_path, notify_on=["success"], notify_channel="wechat-self")

    fake_proc = type("P", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    class _BoomProvider:
        async def send(self, **kw):
            raise RuntimeError("network down")

    with patch("praxdaily.routes.cron.shutil.which", return_value="/fake/prax"), \
         patch("praxdaily.routes.cron.subprocess.run", return_value=fake_proc), \
         patch("prax.tools.notify.build_provider", return_value=_BoomProvider()):
        r = client.post("/api/cron/news/trigger-now")
    assert r.status_code == 200
    notify = r.json()["notify"]
    assert notify["sent"] is False
    assert "RuntimeError" in notify["error"]
    assert "network down" in notify["error"]
