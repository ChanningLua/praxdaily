"""Sources route tests."""

from __future__ import annotations

from pathlib import Path

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


def test_get_sources_returns_defaults_when_no_file(tmp_path):
    """A fresh cwd must return the same defaults the skill would use, so
    the GUI shows users 'this is what would happen right now'."""
    r = _client(tmp_path).get("/api/sources")
    assert r.status_code == 200
    data = r.json()
    assert data["is_user_config_present"] is False
    ids = [s["id"] for s in data["sources"]]
    assert ids == ["twitter", "zhihu", "bilibili", "hackernews"]
    # Defaults match SKILL.md's embedded values.
    assert all(s["enabled"] for s in data["sources"])
    assert "AI" in data["keywords"]["include"]


def test_put_writes_yaml_in_skill_compatible_shape(tmp_path):
    client = _client(tmp_path)
    payload = {
        "sources": [
            {"id": "twitter", "enabled": True, "limit": 30, "top_n": 5},
            {"id": "bilibili", "enabled": False, "limit": 20, "top_n": 5},
        ],
        "keywords": {
            "include": ["AI", "agent"],
            "exclude": ["广告", "推广"],
        },
    }
    r = client.put("/api/sources", json=payload)
    assert r.status_code == 200, r.text

    yaml_path = tmp_path / ".prax" / "sources.yaml"
    on_disk = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert on_disk == {
        "sources": [
            {"id": "twitter", "enabled": True, "limit": 30, "top_n": 5},
            {"id": "bilibili", "enabled": False, "limit": 20, "top_n": 5},
        ],
        "keywords": {
            "include": ["AI", "agent"],
            "exclude": ["广告", "推广"],
        },
    }


def test_get_after_put_layers_user_over_defaults(tmp_path):
    """User wrote only twitter — default zhihu/bilibili/hackernews must
    still appear in the GET response so the GUI can render the full
    table without losing un-customized rows."""
    client = _client(tmp_path)
    client.put(
        "/api/sources",
        json={
            "sources": [{"id": "twitter", "enabled": False, "limit": 100, "top_n": 20}],
            "keywords": {"include": ["AI"], "exclude": []},
        },
    )
    data = client.get("/api/sources").json()
    assert data["is_user_config_present"] is True
    by_id = {s["id"]: s for s in data["sources"]}
    # User override applied
    assert by_id["twitter"]["enabled"] is False
    assert by_id["twitter"]["limit"] == 100
    # Defaults preserved for rows the user didn't touch
    assert by_id["zhihu"]["enabled"] is True
    assert by_id["bilibili"]["enabled"] is True


def test_put_rejects_duplicate_source_ids(tmp_path):
    r = _client(tmp_path).put(
        "/api/sources",
        json={
            "sources": [
                {"id": "twitter", "enabled": True, "limit": 10, "top_n": 5},
                {"id": "twitter", "enabled": False, "limit": 20, "top_n": 5},
            ],
            "keywords": {"include": [], "exclude": []},
        },
    )
    assert r.status_code == 400
    assert "duplicate" in r.json()["detail"]


def test_put_rejects_top_n_greater_than_limit(tmp_path):
    r = _client(tmp_path).put(
        "/api/sources",
        json={
            "sources": [{"id": "twitter", "enabled": True, "limit": 5, "top_n": 99}],
            "keywords": {"include": [], "exclude": []},
        },
    )
    assert r.status_code == 400
    assert "top_n" in r.json()["detail"]


def test_put_drops_blank_keyword_strings(tmp_path):
    """Tag-input UIs love sending empty strings — strip them server-side."""
    client = _client(tmp_path)
    client.put(
        "/api/sources",
        json={
            "sources": [{"id": "twitter", "enabled": True, "limit": 10, "top_n": 5}],
            "keywords": {"include": ["AI", "  ", "", "LLM"], "exclude": []},
        },
    )
    data = client.get("/api/sources").json()
    assert data["keywords"]["include"] == ["AI", "LLM"]


def test_delete_resets_to_defaults(tmp_path):
    client = _client(tmp_path)
    # Save something custom
    client.put(
        "/api/sources",
        json={
            "sources": [{"id": "twitter", "enabled": False, "limit": 50, "top_n": 5}],
            "keywords": {"include": ["custom"], "exclude": []},
        },
    )
    assert (tmp_path / ".prax" / "sources.yaml").exists()
    # Reset
    r = client.delete("/api/sources")
    assert r.status_code == 200
    assert not (tmp_path / ".prax" / "sources.yaml").exists()
    # GET now returns DEFAULTS again
    data = client.get("/api/sources").json()
    assert data["is_user_config_present"] is False
    assert data["sources"][0]["enabled"] is True  # twitter back to enabled
    assert "AI" in data["keywords"]["include"]


def test_put_preserves_unknown_user_source_ids(tmp_path):
    """Forward-compat: if a user adds e.g. id=weibo (no autocli mapping
    yet), don't drop it — the skill will skip it but the GUI must still
    show it so the user can delete or change it."""
    client = _client(tmp_path)
    client.put(
        "/api/sources",
        json={
            "sources": [
                *[
                    {"id": sid, "enabled": True, "limit": 10, "top_n": 5}
                    for sid in ("twitter", "zhihu", "bilibili", "hackernews")
                ],
                {"id": "weibo", "enabled": True, "limit": 30, "top_n": 10},
            ],
            "keywords": {"include": [], "exclude": []},
        },
    )
    data = client.get("/api/sources").json()
    ids = [s["id"] for s in data["sources"]]
    assert "weibo" in ids
