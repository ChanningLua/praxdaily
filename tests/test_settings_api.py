"""Settings (env-key editor) route tests.

The single most important invariant here: the API must NEVER return raw
secret values to the browser, only a masked preview. Everything else is
ergonomic.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


pytestmark = pytest.mark.skipif(not FASTAPI_AVAILABLE, reason="fastapi not installed")


def _client(cwd: Path) -> TestClient:
    from praxdaily.app import create_app
    return TestClient(create_app(cwd=cwd))


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")


def test_list_env_returns_empty_when_no_env_file(tmp_path):
    r = _client(tmp_path).get("/api/settings/env")
    assert r.status_code == 200
    data = r.json()
    assert data["exists"] is False
    assert all(k["configured"] is False for k in data["keys"])
    assert data["extras"] == []


def test_set_env_creates_file_with_chmod_600(tmp_path):
    client = _client(tmp_path)
    r = client.put("/api/settings/env", json={"name": "OPENAI_API_KEY", "value": "sk-test-value-1234"})
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True

    env_path = tmp_path / ".prax" / ".env"
    assert env_path.exists()
    content = env_path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-test-value-1234" in content

    # chmod check on POSIX only — Windows skips silently in our writer.
    if os.name == "posix":
        mode = stat.S_IMODE(env_path.stat().st_mode)
        assert mode == 0o600, f"expected 0600 perms, got {oct(mode)}"


def test_list_env_never_returns_raw_value(tmp_path):
    client = _client(tmp_path)
    secret = "sk-do-not-leak-this-supersecret-9999"
    client.put("/api/settings/env", json={"name": "OPENAI_API_KEY", "value": secret})

    r = client.get("/api/settings/env")
    body = r.text
    assert secret not in body, "raw secret leaked in /api/settings/env response"

    # But masked preview should expose just the last 4 chars.
    openai = next(k for k in r.json()["keys"] if k["name"] == "OPENAI_API_KEY")
    assert openai["configured"] is True
    assert openai["preview"] == "…9999"


def test_empty_value_deletes_key(tmp_path):
    client = _client(tmp_path)
    client.put("/api/settings/env", json={"name": "ZHIPU_API_KEY", "value": "abcd1234"})
    client.put("/api/settings/env", json={"name": "ZHIPU_API_KEY", "value": ""})

    r = client.get("/api/settings/env")
    zhipu = next(k for k in r.json()["keys"] if k["name"] == "ZHIPU_API_KEY")
    assert zhipu["configured"] is False
    assert zhipu["preview"] == ""


def test_unknown_env_var_preserved_as_extra(tmp_path):
    """User may have manually written exotic keys (HTTP_PROXY etc); we must
    surface them in `extras` so saving through the UI doesn't silently drop
    them."""
    env = tmp_path / ".prax" / ".env"
    env.parent.mkdir()
    env.write_text("HTTP_PROXY=http://corp:8080\nOPENAI_API_KEY=sk-xxxx1234\n", encoding="utf-8")

    r = _client(tmp_path).get("/api/settings/env")
    extras = r.json()["extras"]
    assert any(e["name"] == "HTTP_PROXY" for e in extras)


def test_set_env_preserves_other_keys(tmp_path):
    client = _client(tmp_path)
    client.put("/api/settings/env", json={"name": "OPENAI_API_KEY", "value": "sk-aaaa"})
    client.put("/api/settings/env", json={"name": "ZHIPU_API_KEY", "value": "zh-bbbb"})

    content = (tmp_path / ".prax" / ".env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-aaaa" in content
    assert "ZHIPU_API_KEY=zh-bbbb" in content


def test_set_env_rejects_lowercase_or_invalid_name(tmp_path):
    client = _client(tmp_path)
    r = client.put("/api/settings/env", json={"name": "openai_api_key", "value": "x"})
    assert r.status_code == 400
    r = client.put("/api/settings/env", json={"name": "BAD-NAME", "value": "x"})
    assert r.status_code == 400


def test_value_with_spaces_round_trips(tmp_path):
    """If a user pastes a value containing spaces (rare but happens with
    proxy URLs), we quote on write and unquote on read."""
    client = _client(tmp_path)
    client.put("/api/settings/env", json={"name": "OPENAI_API_KEY", "value": "sk has spaces 1234"})

    r = client.get("/api/settings/env")
    openai = next(k for k in r.json()["keys"] if k["name"] == "OPENAI_API_KEY")
    assert openai["preview"] == "…1234"


def test_doctor_returns_subprocess_output(tmp_path, monkeypatch):
    """Don't actually require prax installed in tests; stub the subprocess."""
    import praxdaily.routes.settings as settings_mod

    class _FakeProc:
        returncode = 0
        stdout = "glm: ok\nclaude: missing-key\n"
        stderr = ""

    def fake_run(cmd, **kw):
        assert cmd == ["prax", "doctor", "all"]
        return _FakeProc()

    monkeypatch.setattr(settings_mod.subprocess, "run", fake_run)
    r = _client(tmp_path).get("/api/settings/doctor")
    assert r.status_code == 200
    body = r.json()
    assert body["exit_code"] == 0
    assert "glm: ok" in body["stdout"]


def test_list_env_returns_default_base_urls_when_no_override(tmp_path):
    r = _client(tmp_path).get("/api/settings/env")
    keys = {k["name"]: k for k in r.json()["keys"]}
    assert keys["OPENAI_API_KEY"]["base_url_override"] == ""
    assert keys["OPENAI_API_KEY"]["effective_base_url"] == "https://api.openai.com/v1"
    assert keys["ANTHROPIC_API_KEY"]["effective_base_url"] == "https://api.anthropic.com"


def test_set_provider_writes_only_base_url_field(tmp_path):
    """User-yaml deep-merges with bundled, so we must write *only* base_url —
    not 'models' or 'format' — otherwise we'd shadow the bundled list."""
    client = _client(tmp_path)
    r = client.put("/api/settings/provider", json={
        "provider": "openai",
        "base_url": "https://apikey.soxio.me/openai/v1",
    })
    assert r.status_code == 200, r.text

    import yaml as _yaml
    raw = (Path.home() / ".prax" / "models.yaml").read_text(encoding="utf-8")
    data = _yaml.safe_load(raw)
    assert data == {"providers": {"openai": {"base_url": "https://apikey.soxio.me/openai/v1"}}}


def test_set_provider_preserves_other_providers(tmp_path):
    """Setting openai must not wipe a previously-set anthropic override."""
    client = _client(tmp_path)
    client.put("/api/settings/provider", json={"provider": "anthropic", "base_url": "https://relay.example.com"})
    client.put("/api/settings/provider", json={"provider": "openai", "base_url": "https://api.example.com/v1"})

    import yaml as _yaml
    data = _yaml.safe_load((Path.home() / ".prax" / "models.yaml").read_text())
    assert data["providers"]["anthropic"]["base_url"] == "https://relay.example.com"
    assert data["providers"]["openai"]["base_url"] == "https://api.example.com/v1"


def test_set_provider_empty_url_removes_override(tmp_path):
    client = _client(tmp_path)
    client.put("/api/settings/provider", json={"provider": "openai", "base_url": "https://x.com/v1"})
    client.put("/api/settings/provider", json={"provider": "openai", "base_url": ""})

    # File should either not exist or have an empty providers section.
    p = Path.home() / ".prax" / "models.yaml"
    if p.exists():
        import yaml as _yaml
        data = _yaml.safe_load(p.read_text()) or {}
        assert "openai" not in (data.get("providers") or {})


def test_set_provider_preserves_user_unrelated_fields(tmp_path):
    """If user has hand-crafted `format: responses` or other fields under
    a provider, we must not nuke them when changing base_url."""
    p = Path.home() / ".prax" / "models.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "providers:\n"
        "  openai:\n"
        "    base_url: https://old.example.com\n"
        "    format: openai\n"
        "    custom_field: keep-me\n"
        "default_model: gpt-5.4\n",
        encoding="utf-8",
    )

    _client(tmp_path).put("/api/settings/provider", json={
        "provider": "openai", "base_url": "https://new.example.com/v1",
    })

    import yaml as _yaml
    data = _yaml.safe_load(p.read_text())
    assert data["providers"]["openai"]["base_url"] == "https://new.example.com/v1"
    assert data["providers"]["openai"]["format"] == "openai"
    assert data["providers"]["openai"]["custom_field"] == "keep-me"
    assert data["default_model"] == "gpt-5.4"


def test_set_provider_rejects_unknown_provider(tmp_path):
    r = _client(tmp_path).put("/api/settings/provider", json={"provider": "ollama", "base_url": "http://x"})
    assert r.status_code == 400


def test_set_provider_rejects_non_http_url(tmp_path):
    r = _client(tmp_path).put("/api/settings/provider", json={"provider": "openai", "base_url": "ftp://x"})
    assert r.status_code == 400


def test_list_env_reflects_provider_override(tmp_path):
    client = _client(tmp_path)
    client.put("/api/settings/provider", json={"provider": "openai", "base_url": "https://apikey.soxio.me/openai/v1"})

    keys = {k["name"]: k for k in client.get("/api/settings/env").json()["keys"]}
    assert keys["OPENAI_API_KEY"]["base_url_override"] == "https://apikey.soxio.me/openai/v1"
    assert keys["OPENAI_API_KEY"]["effective_base_url"] == "https://apikey.soxio.me/openai/v1"
    # Anthropic still default
    assert keys["ANTHROPIC_API_KEY"]["base_url_override"] == ""


# ── 0.7: workspace-yaml ghost-file detection + cleanup ──────────────────────


def _write_workspace_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / ".prax" / "models.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_no_conflict_when_workspace_yaml_absent(tmp_path):
    client = _client(tmp_path)
    client.put("/api/settings/provider", json={"provider": "openai", "base_url": "https://relay.example.com/v1"})
    keys = {k["name"]: k for k in client.get("/api/settings/env").json()["keys"]}
    assert keys["OPENAI_API_KEY"]["conflict"] is None
    assert keys["OPENAI_API_KEY"]["effective_base_url"] == "https://relay.example.com/v1"


def test_no_conflict_when_workspace_overrides_match_user(tmp_path):
    client = _client(tmp_path)
    client.put("/api/settings/provider", json={"provider": "openai", "base_url": "https://relay.example.com/v1"})
    _write_workspace_yaml(tmp_path, """
providers:
  openai:
    base_url: https://relay.example.com/v1
""")
    keys = {k["name"]: k for k in client.get("/api/settings/env").json()["keys"]}
    assert keys["OPENAI_API_KEY"]["conflict"] is None


def test_conflict_when_workspace_overrides_user(tmp_path):
    """The exact bug we hit: user configures soxio relay, but a ghost
    workspace yaml from `prax /init-models` puts api.openai.com on top."""
    client = _client(tmp_path)
    client.put("/api/settings/provider", json={"provider": "openai", "base_url": "https://apikey.soxio.me/openai"})
    _write_workspace_yaml(tmp_path, """
providers:
  openai:
    base_url: https://api.openai.com/v1
""")

    keys = {k["name"]: k for k in client.get("/api/settings/env").json()["keys"]}
    openai = keys["OPENAI_API_KEY"]
    # effective must reflect what prax actually uses (workspace wins)
    assert openai["effective_base_url"] == "https://api.openai.com/v1"
    # user's intent is preserved separately so the UI can show "you set X but it's not in effect"
    assert openai["base_url_override"] == "https://apikey.soxio.me/openai"
    # conflict flagged with both values
    c = openai["conflict"]
    assert c is not None
    assert c["kind"] == "base_url_overridden"
    assert c["user_value"] == "https://apikey.soxio.me/openai"
    assert c["workspace_value"] == "https://api.openai.com/v1"
    assert "init-models" in c["message"]


def test_no_conflict_when_user_layer_unset(tmp_path):
    """If user hasn't set anything, workspace winning over bundled is by
    design — that's not a conflict, that's the feature."""
    _write_workspace_yaml(tmp_path, """
providers:
  openai:
    base_url: https://workspace-only.example.com/v1
""")
    keys = {k["name"]: k for k in _client(tmp_path).get("/api/settings/env").json()["keys"]}
    assert keys["OPENAI_API_KEY"]["conflict"] is None
    assert keys["OPENAI_API_KEY"]["effective_base_url"] == "https://workspace-only.example.com/v1"


def test_cleanup_workspace_yaml_creates_timestamped_backup(tmp_path):
    p = _write_workspace_yaml(tmp_path, "providers:\n  openai:\n    base_url: https://x/\n")
    r = _client(tmp_path).post("/api/settings/cleanup-workspace-yaml")
    assert r.status_code == 200, r.text
    assert not p.exists(), "workspace yaml should be removed"
    body = r.json()
    assert body["ok"] is True
    backup = Path(body["backup"])
    assert backup.exists(), "backup should be present"
    assert ".yaml.bak." in backup.name
    # content preserved verbatim
    assert "https://x/" in backup.read_text(encoding="utf-8")


def test_cleanup_404_when_no_workspace_yaml(tmp_path):
    r = _client(tmp_path).post("/api/settings/cleanup-workspace-yaml")
    assert r.status_code == 404


def test_cleanup_resolves_conflict(tmp_path):
    """End-to-end: detect conflict → cleanup → conflict gone, user value lives."""
    client = _client(tmp_path)
    client.put("/api/settings/provider", json={"provider": "openai", "base_url": "https://soxio.example.com/openai"})
    _write_workspace_yaml(tmp_path, "providers:\n  openai:\n    base_url: https://api.openai.com/v1\n")

    before = client.get("/api/settings/env").json()
    assert before["keys"][0]["conflict"] is not None  # OPENAI_API_KEY first

    client.post("/api/settings/cleanup-workspace-yaml")

    after = client.get("/api/settings/env").json()
    openai = next(k for k in after["keys"] if k["name"] == "OPENAI_API_KEY")
    assert openai["conflict"] is None
    assert openai["effective_base_url"] == "https://soxio.example.com/openai"


def test_workspace_yaml_with_invalid_syntax_is_flagged_not_crashed(tmp_path):
    _write_workspace_yaml(tmp_path, "providers:\n  openai:\n    base_url: [unclosed")
    r = _client(tmp_path).get("/api/settings/env")
    assert r.status_code == 200, "settings page must not crash on bad workspace yaml"
    keys = r.json()["keys"]
    # The parse_error conflict is shown on every provider since we can't tell
    # what the file would have set per-provider.
    assert any(k["conflict"] and k["conflict"]["kind"] == "parse_error" for k in keys)


# ── 0.7: base_url probe (catch typos before saving) ─────────────────────────


def _stub_httpx_response(status_code: int, text: str = ""):
    """Return a fake httpx response object compatible with the probe code."""
    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.text = text
    return _Resp()


def _patch_probe(monkeypatch, *, status: int = 200, text: str = "", raise_exc=None, statuses: list[int] | None = None):
    """Patch httpx.Client used inside the probe endpoint.

    `statuses`: optional list to return different statuses across successive
    GET/POST calls (the probe falls back from GET /models to POST /chat/completions
    on 404, so some tests need to control both responses).
    """
    captured = {"calls": []}
    seq = list(statuses) if statuses else None

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def _do(self, method, url, headers=None, json=None):
            captured["calls"].append({"method": method, "url": url, "headers": headers or {}, "json": json})
            captured["url"] = url; captured["headers"] = headers or {}  # legacy keys for older tests
            if raise_exc:
                raise raise_exc
            this_status = seq.pop(0) if seq else status
            return _stub_httpx_response(this_status, text)
        def get(self, url, headers=None): return self._do("GET", url, headers=headers)
        def post(self, url, headers=None, json=None): return self._do("POST", url, headers=headers, json=json)

    import httpx
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return captured


def test_probe_returns_ok_for_2xx(tmp_path, monkeypatch):
    captured = _patch_probe(monkeypatch, status=200, text='{"data":[]}')
    r = _client(tmp_path).post(
        "/api/settings/probe",
        json={"provider": "openai", "base_url": "https://relay.example.com/v1"},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["http_status"] == 200
    assert captured["url"] == "https://relay.example.com/v1/models"


def test_probe_treats_401_as_endpoint_exists(tmp_path, monkeypatch):
    """401/403 means URL is right, key/auth is wrong — much better signal
    than failing the URL save outright."""
    _patch_probe(monkeypatch, status=401, text="Unauthorized")
    r = _client(tmp_path).post(
        "/api/settings/probe",
        json={"provider": "openai", "base_url": "https://relay.example.com/v1"},
    )
    body = r.json()
    assert body["ok"] is True  # endpoint exists, just auth fails
    assert "key" in body["message"]


def test_probe_404_suggests_adding_v1(tmp_path, monkeypatch):
    """The exact bug we hit: user pasted https://apikey.soxio.me/openai
    (no /v1). GET /models 404, then POST /chat/completions also 404 →
    probe should hint at the fix."""
    _patch_probe(monkeypatch, statuses=[404, 404])
    r = _client(tmp_path).post(
        "/api/settings/probe",
        json={"provider": "openai", "base_url": "https://apikey.soxio.me/openai"},
    )
    body = r.json()
    assert body["ok"] is False
    assert body["http_status"] == 404
    assert "/v1" in body["error"]
    assert "https://apikey.soxio.me/openai/v1" in body["error"]


def test_probe_404_no_v1_hint_when_already_v1(tmp_path, monkeypatch):
    _patch_probe(monkeypatch, statuses=[404, 404])
    r = _client(tmp_path).post(
        "/api/settings/probe",
        json={"provider": "openai", "base_url": "https://x.example.com/v1"},
    )
    body = r.json()
    assert body["ok"] is False
    assert "/v1" not in body["error"]  # already has v1, don't suggest it again


def test_probe_falls_back_to_chat_completions_when_models_404(tmp_path, monkeypatch):
    """The soxio case: /models doesn't exist, but /chat/completions does
    (returns 400 because we passed stream:true with a fake key). The fallback
    should treat that as "URL is right" — much more useful than just saying
    /models 404."""
    captured = _patch_probe(monkeypatch, statuses=[404, 400])
    r = _client(tmp_path).post(
        "/api/settings/probe",
        json={"provider": "openai", "base_url": "https://apikey.soxio.me/openai/v1"},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["http_status"] == 400
    # Should have made two attempts (the fallback)
    assert len(captured["calls"]) == 2
    assert captured["calls"][0]["method"] == "GET"
    assert captured["calls"][0]["url"].endswith("/models")
    assert captured["calls"][1]["method"] == "POST"
    assert captured["calls"][1]["url"].endswith("/chat/completions")


def test_probe_handles_connect_error(tmp_path, monkeypatch):
    import httpx
    _patch_probe(monkeypatch, raise_exc=httpx.ConnectError("DNS fail"))
    r = _client(tmp_path).post(
        "/api/settings/probe",
        json={"provider": "openai", "base_url": "https://nonexistent.example.com/v1"},
    )
    body = r.json()
    assert body["ok"] is False
    assert "连不上" in body["error"]


def test_probe_uses_x_api_key_header_for_anthropic(tmp_path, monkeypatch):
    """OpenAI-compat uses Authorization: Bearer; Anthropic uses x-api-key
    + anthropic-version. A probe with the wrong headers would falsely
    report failure."""
    captured = _patch_probe(monkeypatch, status=400)  # 400 = "URL exists, payload off"
    client = _client(tmp_path)
    client.put("/api/settings/env", json={"name": "ANTHROPIC_API_KEY", "value": "sk-ant-test"})
    client.post(
        "/api/settings/probe",
        json={"provider": "anthropic", "base_url": "https://api.anthropic.com"},
    )
    headers = captured["calls"][0]["headers"]
    assert headers.get("x-api-key") == "sk-ant-test"
    assert headers.get("anthropic-version") == "2023-06-01"
    assert "Authorization" not in headers
    # Anthropic uses POST /v1/messages, not GET /models
    assert captured["calls"][0]["method"] == "POST"
    assert captured["calls"][0]["url"].endswith("/v1/messages")


def test_probe_uses_bearer_for_openai_with_key(tmp_path, monkeypatch):
    captured = _patch_probe(monkeypatch, status=200)
    client = _client(tmp_path)
    client.put("/api/settings/env", json={"name": "OPENAI_API_KEY", "value": "sk-foo"})
    client.post(
        "/api/settings/probe",
        json={"provider": "openai", "base_url": "https://r.example.com/v1"},
    )
    assert captured["headers"].get("Authorization") == "Bearer sk-foo"


def test_probe_works_without_api_key(tmp_path, monkeypatch):
    """User might want to test connectivity before saving the key. No
    Authorization header should still hit the URL — relay will just
    return 401, which we treat as 'endpoint exists'."""
    captured = _patch_probe(monkeypatch, status=401)
    r = _client(tmp_path).post(
        "/api/settings/probe",
        json={"provider": "openai", "base_url": "https://r.example.com/v1"},
    )
    assert r.json()["ok"] is True  # 401 → endpoint exists
    assert "Authorization" not in captured["headers"]


def test_probe_rejects_unknown_provider(tmp_path):
    r = _client(tmp_path).post(
        "/api/settings/probe",
        json={"provider": "ollama", "base_url": "http://localhost:11434"},
    )
    assert r.status_code == 400


def test_probe_rejects_non_http_url(tmp_path):
    r = _client(tmp_path).post(
        "/api/settings/probe",
        json={"provider": "openai", "base_url": "ftp://example.com/v1"},
    )
    body = r.json()
    assert body["ok"] is False
    assert "http://" in body["error"]


def test_doctor_503_when_prax_missing(tmp_path, monkeypatch):
    import praxdaily.routes.settings as settings_mod

    def fake_run(cmd, **kw):
        raise FileNotFoundError("prax not on PATH")

    monkeypatch.setattr(settings_mod.subprocess, "run", fake_run)
    r = _client(tmp_path).get("/api/settings/doctor")
    assert r.status_code == 503
    assert "PATH" in r.json()["detail"]
