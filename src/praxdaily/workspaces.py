"""Workspace registry — list of known project cwds + the active selection.

Persisted to ``~/.praxdaily/workspaces.json`` so the choice survives
server restarts. Schema:

  {
    "current": "/Users/liuchuanming/projects/my-news",
    "known":   ["/Users/liuchuanming/projects/my-news",
                "/Users/liuchuanming/projects/team-digest"]
  }

Design notes:
- The server starts with ``--cwd X`` (or default cwd if no flag).
  That value seeds the registry on first run; afterwards the registry
  is the source of truth.
- All workspace paths must be absolute, exist as directories, and not
  be one of the obviously-dangerous system paths (rooted at ``/``,
  ``/System``, ``/usr``, ``/private/etc`` etc.). The blocklist is
  conservative — power users can edit the JSON file manually if they
  really want to register one of these.
- Routes resolve the active cwd via ``current_cwd(fallback)`` rather
  than reading ``request.app.state.cwd`` directly.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TypedDict


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically — temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class WorkspaceState(TypedDict):
    current: str
    known: list[str]


_BLOCKED_PREFIXES = (
    "/System", "/usr", "/sbin", "/bin", "/private/etc",
    "/private/var/db", "/private/var/log",
    "/Library/Frameworks",
)


def _state_dir() -> Path:
    return Path.home() / ".praxdaily"


def _state_path() -> Path:
    return _state_dir() / "workspaces.json"


def _is_safe_workspace_path(path: str) -> tuple[bool, str]:
    """Return (ok, reason). The reason is shown to the user on rejection."""
    if not path:
        return False, "path is empty"
    p = Path(path).expanduser()
    if not p.is_absolute():
        return False, "path must be absolute (start with /)"
    s = str(p)
    if s == "/":
        return False, "refusing to register / as a workspace"
    for prefix in _BLOCKED_PREFIXES:
        if s == prefix or s.startswith(prefix + "/"):
            return False, f"path is inside protected system area {prefix!r}"
    if not p.exists():
        return False, f"path does not exist: {s}"
    if not p.is_dir():
        return False, f"path is not a directory: {s}"
    if not os.access(p, os.W_OK):
        return False, f"path is not writable: {s}"
    return True, ""


def load_state(default_cwd: str | None = None) -> WorkspaceState:
    path = _state_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            current = str(data.get("current") or "")
            known = [str(p) for p in (data.get("known") or []) if isinstance(p, str)]
            # If the persisted current is not in known, fall back to the first
            # known entry — protects against manual edits that desync the two.
            if current and current not in known:
                known = [current] + [k for k in known if k != current]
            elif not current and known:
                current = known[0]
            if current and known:
                return {"current": current, "known": known}
        except Exception:
            # Corrupt registry — fall through and reseed below.
            pass

    if not default_cwd:
        default_cwd = str(Path.cwd())
    seed = str(Path(default_cwd).resolve())
    return {"current": seed, "known": [seed]}


def save_state(state: WorkspaceState) -> None:
    _atomic_write_json(_state_path(), dict(state))


def add_workspace(path: str, *, default_cwd: str | None = None) -> WorkspaceState:
    """Register a new workspace and select it. Idempotent."""
    ok, reason = _is_safe_workspace_path(path)
    if not ok:
        raise ValueError(reason)
    resolved = str(Path(path).expanduser().resolve())
    state = load_state(default_cwd=default_cwd)
    if resolved not in state["known"]:
        state["known"].append(resolved)
    state["current"] = resolved
    save_state(state)
    return state


def remove_workspace(path: str, *, default_cwd: str | None = None) -> WorkspaceState:
    """Drop a workspace from the registry. If it was current, fall back
    to the first remaining one (or the default cwd if list goes empty)."""
    state = load_state(default_cwd=default_cwd)
    state["known"] = [k for k in state["known"] if k != path]
    if state["current"] == path:
        if state["known"]:
            state["current"] = state["known"][0]
        else:
            seed = str(Path(default_cwd or Path.cwd()).resolve())
            state["current"] = seed
            state["known"] = [seed]
    save_state(state)
    return state


def select_workspace(path: str, *, default_cwd: str | None = None) -> WorkspaceState:
    """Switch the active workspace. Path must already be in known list."""
    state = load_state(default_cwd=default_cwd)
    if path not in state["known"]:
        raise ValueError(f"workspace {path!r} not registered — add it first")
    state["current"] = path
    save_state(state)
    return state


def current_cwd(*, default_cwd: str) -> str:
    """The cwd routes should treat as 'the active workspace'."""
    return load_state(default_cwd=default_cwd)["current"]
