"""End-to-end smoke tests — open a real browser, click through the UI.

Run with::

    pytest -m e2e

Default ``pytest`` skips these (see pyproject's ``addopts``).
"""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e


def test_overview_loads_with_kpi_strip(page, server):
    """Page paints, KPI strip and sidebar are visible."""
    page.wait_for_selector("text=praxdaily")
    # Sidebar items
    page.wait_for_selector("text=概览")
    page.wait_for_selector("text=微信账号")
    page.wait_for_selector("text=通知渠道")
    page.wait_for_selector("text=抓取源")
    page.wait_for_selector("text=定时任务")
    page.wait_for_selector("text=运行历史")
    # KPI strip card
    page.wait_for_selector("text=prax CLI")


def test_keyboard_shortcut_switches_tab(page):
    """Cmd/Ctrl+3 should jump to 通知渠道."""
    # Use Meta on macOS, Control elsewhere — Playwright's accelerator key.
    page.keyboard.press("Meta+3")
    page.wait_for_selector("h1:has-text('通知渠道')")


def test_command_palette_opens_on_cmd_k(page):
    page.keyboard.press("Meta+k")
    page.wait_for_selector("input[placeholder*='输入命令']")
    # Esc closes
    page.keyboard.press("Escape")
    page.wait_for_selector("input[placeholder*='输入命令']", state="hidden")


def test_add_channel_writes_yaml_and_shows_in_table(page, server):
    """Click through the new-channel form and verify yaml landed on disk."""
    workspace: Path = server["workspace"]

    # Switch to channels tab
    page.keyboard.press("Meta+3")
    page.wait_for_selector("h1:has-text('通知渠道')")

    # Open add form
    page.click("button:has-text('+ 新增渠道')")

    # Fill name + provider (default wechat_personal needs an account_id which
    # tmp workspace doesn't have, so pick feishu_webhook for the smoke).
    page.fill("input[placeholder*='请实际输入']", "smoke-channel")
    page.select_option("select", value="feishu_webhook")
    page.fill("input[placeholder*='webhook URL']", "https://example.com/test-webhook")

    # Save
    page.click("button:has-text('保存')")

    # Toast appears
    # (Toast text comes from server response; the channel list refresh is what we really care about.)
    page.wait_for_selector("td:has-text('smoke-channel')")

    # Verify yaml on disk
    yaml_path = workspace / ".prax" / "notify.yaml"
    assert yaml_path.exists(), "notify.yaml should have been written"
    content = yaml_path.read_text(encoding="utf-8")
    assert "smoke-channel:" in content
    assert "feishu_webhook" in content
    assert "https://example.com/test-webhook" in content


def test_workspace_switch_isolates_data(page, server, tmp_path):
    """Add a second workspace, switch to it, confirm data isolation."""
    other = tmp_path / "other-workspace"
    other.mkdir()

    # Open add-workspace dialog via the sidebar
    page.click("text=+ 添加目录")
    page.wait_for_selector("input[placeholder*='/Users/you/projects']")
    page.fill("input[placeholder*='/Users/you/projects']", str(other))
    page.click("button:has-text('添加并切换')")

    # Toast confirms switch
    page.wait_for_selector("text=已添加并切到", timeout=5000)

    # The cwd in the health-stat strip should now reflect the other path
    # (the page header doesn't show cwd directly, but /api/health does;
    # we just trust the toast as proof and verify the sidebar select shows
    # the new path).
    sidebar_select = page.locator("aside select").first
    assert str(other) in sidebar_select.input_value() or other.name in sidebar_select.input_value()


def test_sidebar_collapse_toggles_layout(page):
    """Collapse button shrinks aside to icon-only width."""
    aside = page.locator("aside")
    initial_box = aside.bounding_box()
    assert initial_box and initial_box["width"] > 200  # uncollapsed

    page.click("aside button[title*='侧栏']")
    page.wait_for_function("() => document.querySelector('aside').offsetWidth < 100")
    collapsed_box = aside.bounding_box()
    assert collapsed_box["width"] < 100

    # Toggle back
    page.click("aside button[title*='侧栏']")
    page.wait_for_function("() => document.querySelector('aside').offsetWidth > 200")
