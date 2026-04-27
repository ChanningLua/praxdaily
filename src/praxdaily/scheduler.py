"""praxdaily's own LaunchAgent (macOS) / crontab (Linux) installer.

We used to piggyback on prax's `prax cron` dispatcher, which routed
each scheduled run through `prax prompt` + LLM-driven skill execution.
That made scheduled runs behaviourally different from manual ones
(different code path, different failure modes — the LLM might decide
to do "extra" steps that crash). Owning our own dispatcher fixes that:
both paths invoke ``runner.run_once()`` which calls
``pipeline.run()`` directly.

Currently macOS only — Linux crontab will land when we have a Linux
user to test against. macOS is the dev box and what the user has.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Distinct from prax's `dev.prax.cron.dispatcher` so the two can coexist
# during migration (and so we never accidentally clobber the user's
# other prax projects).
LAUNCHD_LABEL = "com.praxdaily.daily"


@dataclass
class Schedule:
    """A simple "every day at HH:MM" schedule.

    We deliberately don't expose full cron expressions — daily-digest
    use case is "fire once a day at a fixed time" 99% of cases, and
    cron expression UX is famously confusing. Power users who really
    want crontab can edit the plist by hand.
    """
    hour: int     # 0-23
    minute: int   # 0-59

    def __post_init__(self) -> None:
        if not (0 <= self.hour <= 23):
            raise ValueError(f"hour must be 0-23, got {self.hour}")
        if not (0 <= self.minute <= 59):
            raise ValueError(f"minute must be 0-59, got {self.minute}")

    @classmethod
    def parse_hhmm(cls, hhmm: str) -> "Schedule":
        """Parse ``"HH:MM"`` into a Schedule."""
        s = hhmm.strip()
        if ":" not in s:
            raise ValueError(f"schedule must look like 'HH:MM', got {hhmm!r}")
        h, m = s.split(":", 1)
        return cls(hour=int(h), minute=int(m))

    def to_hhmm(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _render_plist(*, schedule: Schedule, cwd: Path, log_dir: Path) -> str:
    """Write the LaunchAgent plist. Uses ``sys.executable -m praxdaily``
    so it survives praxdaily upgrades — the entry point resolves at
    runtime, not at install time."""
    cwd_str = str(cwd.resolve())
    stdout_log = str(log_dir / "praxdaily-schedule.stdout.log")
    stderr_log = str(log_dir / "praxdaily-schedule.stderr.log")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>-m</string>
        <string>praxdaily</string>
        <string>run-now</string>
        <string>--cwd</string>
        <string>{cwd_str}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{schedule.hour}</integer>
        <key>Minute</key>
        <integer>{schedule.minute}</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>{cwd_str}</string>
    <key>StandardOutPath</key>
    <string>{stdout_log}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_log}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""


def install(*, schedule: Schedule, cwd: Path) -> dict[str, Any]:
    """Install (or replace) the LaunchAgent. Idempotent.

    Always reloads via ``launchctl unload`` then ``launchctl load`` so
    a second install with a new schedule actually takes effect — load
    alone is a no-op for an already-loaded label.
    """
    if platform.system() != "Darwin":
        raise NotImplementedError(
            f"praxdaily scheduler currently macOS-only; got {platform.system()}"
        )

    plist_path = launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    log_dir = Path(cwd) / ".prax" / "logs" / "schedule"
    log_dir.mkdir(parents=True, exist_ok=True)

    plist_path.write_text(_render_plist(schedule=schedule, cwd=Path(cwd), log_dir=log_dir), encoding="utf-8")

    # Best-effort unload first (silently ignore "not loaded" errors); then load.
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True, text=True,
    )
    load = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True, text=True,
    )
    return {
        "installed": True,
        "label": LAUNCHD_LABEL,
        "plist_path": str(plist_path),
        "schedule": schedule.to_hhmm(),
        "log_dir": str(log_dir),
        "launchctl_returncode": load.returncode,
        "launchctl_stderr": load.stderr.strip(),
    }


def uninstall() -> dict[str, Any]:
    """Unload and remove the plist. Safe to call when nothing's installed."""
    if platform.system() != "Darwin":
        raise NotImplementedError("praxdaily scheduler currently macOS-only")
    plist_path = launchd_plist_path()
    if not plist_path.exists():
        return {"installed": False, "removed": False, "label": LAUNCHD_LABEL}
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True, text=True,
    )
    plist_path.unlink()
    return {"installed": False, "removed": True, "label": LAUNCHD_LABEL, "plist_path": str(plist_path)}


def status() -> dict[str, Any]:
    """Report current install state — what the GUI shows on the schedule tab."""
    plist_path = launchd_plist_path()
    if not plist_path.exists():
        return {"installed": False, "label": LAUNCHD_LABEL}

    text = plist_path.read_text(encoding="utf-8")
    # Extract schedule by simple regex; it's our own plist, the format is fixed.
    import re
    h_match = re.search(r"<key>Hour</key>\s*<integer>(\d+)</integer>", text)
    m_match = re.search(r"<key>Minute</key>\s*<integer>(\d+)</integer>", text)
    schedule = ""
    if h_match and m_match:
        schedule = f"{int(h_match.group(1)):02d}:{int(m_match.group(1)):02d}"

    cwd_match = re.search(r"<key>WorkingDirectory</key>\s*<string>([^<]+)</string>", text)
    cwd = cwd_match.group(1) if cwd_match else ""

    # Loaded into launchd?
    listed = subprocess.run(
        ["launchctl", "list", LAUNCHD_LABEL],
        capture_output=True, text=True,
    )
    loaded = listed.returncode == 0

    return {
        "installed": True,
        "loaded": loaded,
        "label": LAUNCHD_LABEL,
        "plist_path": str(plist_path),
        "schedule": schedule,
        "cwd": cwd,
    }


# ── Migration helpers ──────────────────────────────────────────────────────


PRAX_CRON_LABEL = "dev.prax.cron.dispatcher"


def detect_prax_cron_dispatcher() -> dict[str, Any]:
    """Was prax's per-minute dispatcher installed? If yes, the user
    probably set things up before praxdaily owned scheduling. The GUI
    surfaces this so the user can replace it (otherwise both fire and
    they get duplicate runs)."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{PRAX_CRON_LABEL}.plist"
    if not plist_path.exists():
        return {"present": False}
    listed = subprocess.run(
        ["launchctl", "list", PRAX_CRON_LABEL],
        capture_output=True, text=True,
    )
    return {
        "present": True,
        "loaded": listed.returncode == 0,
        "plist_path": str(plist_path),
    }


def uninstall_prax_cron_dispatcher() -> dict[str, Any]:
    """Tear down the legacy `prax cron` dispatcher. Used by the GUI's
    "switch to praxdaily scheduling" flow so the user doesn't end up
    with both schedulers firing."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{PRAX_CRON_LABEL}.plist"
    if not plist_path.exists():
        return {"removed": False, "reason": "not installed"}
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True, text=True,
    )
    plist_path.unlink()
    return {"removed": True, "plist_path": str(plist_path)}
