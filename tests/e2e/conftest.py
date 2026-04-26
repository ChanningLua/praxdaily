"""Shared fixtures for end-to-end browser tests.

These tests are gated behind ``pytest -m e2e`` and require::

    pip install -e ".[e2e]"
    playwright install chromium

The fixture spins up a real ``praxdaily serve`` process on a random
free port pointed at a tmp_path workspace, and a Playwright browser
context that the test drives. We tear both down between tests so each
case starts from a fresh ``.prax/``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


def _free_port() -> int:
    """Bind+release a random free port — small race but acceptable for tests."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A tmp_path with a synthetic .prax/ skeleton (just the dir)."""
    return tmp_path


@pytest.fixture
def server(workspace: Path, monkeypatch):
    """Start a real praxdaily serve subprocess pointed at *workspace*."""
    port = _free_port()
    src_dir = Path(__file__).resolve().parents[2] / "src"

    # Use a fake HOME so workspaces.json doesn't pollute the developer's
    # real ~/.praxdaily/.
    fake_home = workspace / "fake-home"
    fake_home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PYTHONPATH": str(src_dir) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "HOME": str(fake_home),
    }

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "praxdaily", "serve",
            "--no-open", "--port", str(port), "--cwd", str(workspace),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Poll until /api/health responds.
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/api/health", timeout=1.0)
            if r.status_code == 200:
                last_err = None
                break
        except Exception as e:
            last_err = e
        time.sleep(0.2)

    if last_err is not None:
        proc.terminate()
        out, err = proc.communicate(timeout=3)
        raise RuntimeError(
            f"praxdaily serve never came up on {base}: {last_err}\n"
            f"stdout: {out.decode(errors='replace')[:500]}\n"
            f"stderr: {err.decode(errors='replace')[:500]}"
        )

    yield {"base": base, "port": port, "workspace": workspace, "proc": proc}

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def page(server):
    """A Playwright page bound to the running server. Skips gracefully if
    Playwright isn't installed so contributors don't need it for unit tests."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed — `pip install -e '.[e2e]'`")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            pytest.skip(
                f"chromium not installed for playwright "
                f"(`playwright install chromium`): {exc}"
            )
        context = browser.new_context()
        pg = context.new_page()
        pg.goto(server["base"])
        yield pg
        context.close()
        browser.close()
