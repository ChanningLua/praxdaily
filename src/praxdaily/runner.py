"""One-shot ai-news-daily trigger — what `praxdaily run-now` invokes.

Shell-outs to the installed ``prax`` CLI; never imports prax internals
directly so a praxdaily install can survive a praxagent upgrade
without re-pinning.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def run_once(*, cwd: Path) -> int:
    """Trigger the ai-news-daily skill once and return the prax exit code."""
    prax = shutil.which("prax")
    if prax is None:
        print(
            "Error: `prax` not found on PATH. Install it first:\n"
            "  npm install -g praxagent",
        )
        return 127

    argv = [
        prax,
        "prompt",
        "触发 ai-news-daily 技能",
        "--permission-mode",
        "workspace-write",
    ]
    print(f"$ {' '.join(argv)}")
    proc = subprocess.run(argv, cwd=str(cwd))
    return proc.returncode
