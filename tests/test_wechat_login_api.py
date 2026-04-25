"""WeChat login + delete route tests.

iLink HTTP calls are patched so we never hit the real Tencent server
from CI. Account file IO uses the real save_account/delete_account
backed by tmp_path via PRAX_HOME.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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


# ── /login/start ─────────────────────────────────────────────────────────────


def test_login_start_returns_qr(tmp_path):
    fake_qr = {
        "qrcode": "abc123hex",
        "qrcode_img_content": "https://wechat.scan/qr/abc123",
    }

    async def fake_api_get(*args, **kwargs):
        return fake_qr

    with patch("prax.integrations.wechat_ilink.client._api_get", new=fake_api_get):
        r = _client(tmp_path).post("/api/wechat/login/start")
    assert r.status_code == 200
    data = r.json()
    assert data["qrcode_value"] == "abc123hex"
    assert data["qrcode_url"] == "https://wechat.scan/qr/abc123"
    assert data["base_url"].startswith("https://ilinkai.weixin.qq.com")


def test_login_start_falls_back_when_no_url(tmp_path):
    """Some iLink responses omit qrcode_img_content — return the bare hex."""
    async def fake_api_get(*args, **kwargs):
        return {"qrcode": "bare-token-only"}

    with patch("prax.integrations.wechat_ilink.client._api_get", new=fake_api_get):
        r = _client(tmp_path).post("/api/wechat/login/start")
    data = r.json()
    assert data["qrcode_url"] == "bare-token-only"


def test_login_start_502_on_empty_qrcode(tmp_path):
    async def fake_api_get(*args, **kwargs):
        return {"qrcode": ""}

    with patch("prax.integrations.wechat_ilink.client._api_get", new=fake_api_get):
        r = _client(tmp_path).post("/api/wechat/login/start")
    assert r.status_code == 502


def test_login_start_502_on_ilink_error(tmp_path):
    async def fake_api_get(*args, **kwargs):
        raise RuntimeError("network down")

    with patch("prax.integrations.wechat_ilink.client._api_get", new=fake_api_get):
        r = _client(tmp_path).post("/api/wechat/login/start")
    assert r.status_code == 502
    assert "iLink QR fetch failed" in r.json()["detail"]


# ── /login/poll ──────────────────────────────────────────────────────────────


def _poll(client: TestClient, value: str = "abc", base: str = "https://ilinkai.weixin.qq.com"):
    return client.post(
        "/api/wechat/login/poll",
        json={"qrcode_value": value, "base_url": base},
    )


def test_poll_relays_wait(tmp_path):
    async def fake_api_get(*args, **kwargs):
        return {"status": "wait"}

    with patch("prax.integrations.wechat_ilink.client._api_get", new=fake_api_get):
        r = _poll(_client(tmp_path))
    assert r.status_code == 200
    assert r.json() == {"status": "wait"}


def test_poll_relays_scaned(tmp_path):
    async def fake_api_get(*args, **kwargs):
        return {"status": "scaned"}

    with patch("prax.integrations.wechat_ilink.client._api_get", new=fake_api_get):
        r = _poll(_client(tmp_path))
    assert r.json() == {"status": "scaned"}


def test_poll_returns_redirect_host(tmp_path):
    async def fake_api_get(*args, **kwargs):
        return {"status": "scaned_but_redirect", "redirect_host": "ilinkai-cn.example"}

    with patch("prax.integrations.wechat_ilink.client._api_get", new=fake_api_get):
        r = _poll(_client(tmp_path))
    data = r.json()
    assert data["status"] == "scaned_but_redirect"
    assert data["redirect_host"] == "ilinkai-cn.example"
    assert data["next_base_url"] == "https://ilinkai-cn.example"


def test_poll_relays_expired(tmp_path):
    async def fake_api_get(*args, **kwargs):
        return {"status": "expired"}

    with patch("prax.integrations.wechat_ilink.client._api_get", new=fake_api_get):
        r = _poll(_client(tmp_path))
    assert r.json() == {"status": "expired"}


def test_poll_treats_timeout_as_wait(tmp_path):
    """A network blip on the long poll should not surface as a hard error —
    the client just retries on the next 2 s tick."""
    import httpx

    async def fake_api_get(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    with patch("prax.integrations.wechat_ilink.client._api_get", new=fake_api_get):
        r = _poll(_client(tmp_path))
    assert r.status_code == 200
    assert r.json() == {"status": "wait"}


def test_poll_confirmed_persists_account_and_omits_token(tmp_path, monkeypatch):
    """Confirmed must (a) persist credentials to ~/.prax/wechat/, and
    (b) not leak the bot_token back to the browser."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    async def fake_api_get(*args, **kwargs):
        return {
            "status": "confirmed",
            "ilink_bot_id": "ilink_test@im.bot",
            "bot_token": "secret-bot-token-do-not-leak",
            "baseurl": "https://ilinkai.weixin.qq.com",
            "ilink_user_id": "u_self@im.wechat",
        }

    with patch("prax.integrations.wechat_ilink.client._api_get", new=fake_api_get):
        r = _poll(_client(tmp_path))

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "confirmed"
    assert data["account_id"] == "ilink_test@im.bot"
    assert data["user_id"] == "u_self@im.wechat"
    # Critical: the bot_token must NOT travel back to the browser.
    assert "bot_token" not in data
    assert "secret-bot-token-do-not-leak" not in r.text

    # And the file must exist on disk in user-home (which we redirected).
    saved = tmp_path / ".prax" / "wechat" / "ilink_test@im.bot.json"
    assert saved.exists()
    import json
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["token"] == "secret-bot-token-do-not-leak"  # written, but not returned


def test_poll_502_on_confirmed_with_missing_payload(tmp_path):
    async def fake_api_get(*args, **kwargs):
        return {"status": "confirmed"}  # no bot_id / bot_token

    with patch("prax.integrations.wechat_ilink.client._api_get", new=fake_api_get):
        r = _poll(_client(tmp_path))
    assert r.status_code == 502


# ── DELETE /accounts/{account_id} ────────────────────────────────────────────


def test_delete_account_removes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from prax.integrations.wechat_ilink.store import save_account
    save_account(
        account_id="ilink_doomed@im.bot",
        token="t", base_url="u", user_id="x",
    )
    saved = tmp_path / ".prax" / "wechat" / "ilink_doomed@im.bot.json"
    assert saved.exists()

    r = _client(tmp_path).delete("/api/wechat/accounts/ilink_doomed@im.bot")
    assert r.status_code == 200
    assert r.json() == {"deleted": "ilink_doomed@im.bot"}
    assert not saved.exists()


def test_delete_account_404_when_missing(tmp_path):
    r = _client(tmp_path).delete("/api/wechat/accounts/nope")
    assert r.status_code == 404


def test_delete_account_blocks_path_traversal(tmp_path):
    r = _client(tmp_path).delete("/api/wechat/accounts/..hidden")
    assert r.status_code == 400
