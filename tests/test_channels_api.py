"""Channels CRUD + test-send route tests.

`build_provider` is patched so we never actually hit a real WeChat /
Feishu webhook from CI. The yaml file shape is verified end-to-end
against the file the prax CLI will read.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


def test_list_channels_empty(tmp_path):
    r = _client(tmp_path).get("/api/channels")
    assert r.status_code == 200
    assert r.json() == {"channels": []}


def test_upsert_channel_writes_yaml(tmp_path):
    client = _client(tmp_path)
    payload = {
        "provider": "wechat_personal",
        "account_id": "ilink_abc@im.bot",
        "to": "self",
        "default_title_prefix": "[Prax] ",
    }
    r = client.put("/api/channels/my-wechat", json=payload)
    assert r.status_code == 200, r.text

    yaml_path = tmp_path / ".prax" / "notify.yaml"
    assert yaml_path.exists()
    written = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert written == {
        "channels": {
            "my-wechat": {
                "provider": "wechat_personal",
                "account_id": "ilink_abc@im.bot",
                "to": "self",
                "default_title_prefix": "[Prax] ",
            }
        }
    }


def test_upsert_rejects_unknown_provider(tmp_path):
    r = _client(tmp_path).put(
        "/api/channels/badprov", json={"provider": "carrier_pigeon"}
    )
    assert r.status_code == 400
    assert "unsupported provider" in r.json()["detail"]


def test_upsert_rejects_path_traversal_in_name(tmp_path):
    r = _client(tmp_path).put(
        "/api/channels/..%2Fevil", json={"provider": "feishu_webhook", "url": "x"}
    )
    # FastAPI URL decoder + our explicit "/" check both block this.
    assert r.status_code in (400, 404)


def test_list_after_upsert_reflects_changes(tmp_path):
    client = _client(tmp_path)
    client.put(
        "/api/channels/feishu-test",
        json={"provider": "feishu_webhook", "url": "https://x"},
    )
    r = client.get("/api/channels")
    data = r.json()
    assert len(data["channels"]) == 1
    ch = data["channels"][0]
    assert ch["name"] == "feishu-test"
    assert ch["provider"] == "feishu_webhook"
    assert ch["url"] == "https://x"


def test_delete_channel(tmp_path):
    client = _client(tmp_path)
    client.put(
        "/api/channels/x", json={"provider": "feishu_webhook", "url": "https://x"}
    )
    r = client.delete("/api/channels/x")
    assert r.status_code == 200
    assert r.json() == {"deleted": "x"}
    # gone from list
    assert _client(tmp_path).get("/api/channels").json() == {"channels": []}


def test_delete_missing_returns_404(tmp_path):
    r = _client(tmp_path).delete("/api/channels/nope")
    assert r.status_code == 404


def test_test_send_calls_provider(tmp_path):
    client = _client(tmp_path)
    client.put(
        "/api/channels/feishu-test",
        json={"provider": "feishu_webhook", "url": "https://x"},
    )

    fake_provider = AsyncMock()
    fake_provider.send = AsyncMock()
    with patch("prax.tools.notify.build_provider", return_value=fake_provider):
        r = client.post(
            "/api/channels/feishu-test/test",
            json={"title": "hi", "body": "world", "level": "info"},
        )

    assert r.status_code == 200
    assert r.json() == {"sent": True, "channel": "feishu-test"}
    fake_provider.send.assert_awaited_once_with(title="hi", body="world", level="info")


def test_test_send_translates_provider_failure_to_502(tmp_path):
    client = _client(tmp_path)
    client.put(
        "/api/channels/wechat-personal-bad",
        json={"provider": "wechat_personal", "account_id": "ilink_no_login"},
    )

    fake_provider = AsyncMock()
    fake_provider.send = AsyncMock(side_effect=RuntimeError("ret=-2 ..."))
    with patch("prax.tools.notify.build_provider", return_value=fake_provider):
        r = client.post(
            "/api/channels/wechat-personal-bad/test", json={}
        )
    assert r.status_code == 502
    assert "RuntimeError" in r.json()["detail"]
