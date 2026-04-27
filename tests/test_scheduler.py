"""Scheduler tests — plist generation, install/uninstall flow.

We mock ``subprocess.run`` (launchctl calls) and ``Path.home`` so tests
don't actually load anything into the user's launchd. The plist content
is asserted as a string — that's the user-visible artifact whose shape
must stay stable.
"""

from __future__ import annotations

import platform
from pathlib import Path

import pytest

from praxdaily import scheduler


pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="scheduler currently macOS-only",
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")


@pytest.fixture
def _stub_launchctl(monkeypatch):
    """Capture launchctl calls without invoking real launchd."""
    calls = []

    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(argv, *a, **kw):
        calls.append(list(argv))
        return _Result()

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    return calls


# ── Schedule parsing ───────────────────────────────────────────────────────


def test_schedule_parse_hhmm_round_trips():
    s = scheduler.Schedule.parse_hhmm("14:00")
    assert s.hour == 14 and s.minute == 0
    assert s.to_hhmm() == "14:00"


def test_schedule_parse_hhmm_pads_zero():
    s = scheduler.Schedule.parse_hhmm("9:5")
    assert s.to_hhmm() == "09:05"


def test_schedule_rejects_invalid_hour():
    with pytest.raises(ValueError, match="hour"):
        scheduler.Schedule(hour=24, minute=0)


def test_schedule_rejects_invalid_minute():
    with pytest.raises(ValueError, match="minute"):
        scheduler.Schedule(hour=10, minute=60)


def test_schedule_parse_hhmm_rejects_garbage():
    with pytest.raises(ValueError, match="HH:MM"):
        scheduler.Schedule.parse_hhmm("never")


# ── install ────────────────────────────────────────────────────────────────


def test_install_writes_plist_with_correct_schedule(tmp_path, _stub_launchctl):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    result = scheduler.install(schedule=scheduler.Schedule(hour=9, minute=30), cwd=cwd)

    plist_path = Path(result["plist_path"])
    assert plist_path.exists()
    text = plist_path.read_text(encoding="utf-8")

    # Plist contains the schedule
    assert "<integer>9</integer>" in text
    assert "<integer>30</integer>" in text
    # ProgramArguments invokes praxdaily run-now with the right cwd
    assert "praxdaily" in text
    assert "run-now" in text
    assert str(cwd.resolve()) in text
    # Standard label
    assert scheduler.LAUNCHD_LABEL in text
    assert result["schedule"] == "09:30"


def test_install_creates_log_dir_under_workspace(tmp_path, _stub_launchctl):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    scheduler.install(schedule=scheduler.Schedule(hour=14, minute=0), cwd=cwd)
    log_dir = cwd / ".prax" / "logs" / "schedule"
    assert log_dir.exists()


def test_install_unloads_then_loads_via_launchctl(tmp_path, _stub_launchctl):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    scheduler.install(schedule=scheduler.Schedule(hour=14, minute=0), cwd=cwd)

    cmds = [c[:2] for c in _stub_launchctl]
    assert cmds == [["launchctl", "unload"], ["launchctl", "load"]]


def test_install_overwrites_existing_plist(tmp_path, _stub_launchctl):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    scheduler.install(schedule=scheduler.Schedule(hour=8, minute=0), cwd=cwd)
    scheduler.install(schedule=scheduler.Schedule(hour=20, minute=0), cwd=cwd)

    plist = scheduler.launchd_plist_path().read_text()
    assert "<integer>20</integer>" in plist
    assert "<integer>8</integer>" not in plist


# ── uninstall ──────────────────────────────────────────────────────────────


def test_uninstall_removes_existing_plist(tmp_path, _stub_launchctl):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    scheduler.install(schedule=scheduler.Schedule(hour=14, minute=0), cwd=cwd)
    assert scheduler.launchd_plist_path().exists()

    result = scheduler.uninstall()
    assert result["removed"] is True
    assert not scheduler.launchd_plist_path().exists()


def test_uninstall_when_nothing_installed_is_noop(_stub_launchctl):
    result = scheduler.uninstall()
    assert result["removed"] is False


# ── status ─────────────────────────────────────────────────────────────────


def test_status_reports_not_installed(_stub_launchctl):
    s = scheduler.status()
    assert s["installed"] is False


def test_status_extracts_schedule_from_plist(tmp_path, _stub_launchctl):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    scheduler.install(schedule=scheduler.Schedule(hour=7, minute=15), cwd=cwd)

    s = scheduler.status()
    assert s["installed"] is True
    assert s["schedule"] == "07:15"
    assert str(cwd.resolve()) in s["cwd"]


# ── Legacy prax-cron detection ─────────────────────────────────────────────


def test_detect_prax_cron_dispatcher_absent(tmp_path, _stub_launchctl):
    assert scheduler.detect_prax_cron_dispatcher()["present"] is False


def test_detect_prax_cron_dispatcher_present(tmp_path, _stub_launchctl):
    legacy = tmp_path / "fake-home" / "Library" / "LaunchAgents" / f"{scheduler.PRAX_CRON_LABEL}.plist"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("<plist></plist>", encoding="utf-8")

    info = scheduler.detect_prax_cron_dispatcher()
    assert info["present"] is True
    assert info["plist_path"].endswith(f"{scheduler.PRAX_CRON_LABEL}.plist")


def test_uninstall_legacy_prax_cron_removes_file(tmp_path, _stub_launchctl):
    legacy = tmp_path / "fake-home" / "Library" / "LaunchAgents" / f"{scheduler.PRAX_CRON_LABEL}.plist"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("<plist></plist>", encoding="utf-8")

    result = scheduler.uninstall_prax_cron_dispatcher()
    assert result["removed"] is True
    assert not legacy.exists()


def test_uninstall_legacy_when_absent_is_noop(_stub_launchctl):
    result = scheduler.uninstall_prax_cron_dispatcher()
    assert result["removed"] is False
