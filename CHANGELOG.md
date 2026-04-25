# Changelog

All notable changes to praxdaily will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-04-25

### Added
- **Schedule screen** — list / add / delete cron jobs without touching
  `.prax/cron.yaml`. Frequency is picked from four presets (every day at
  HH:MM, every N hours, every N minutes, raw cron expression) that
  compile down to the same 5-field cron string `prax cron` consumes,
  with a live "将编译为：…" preview so users see what they're saving.
- **Dispatcher control** — Install / Uninstall / "立刻跑一次" buttons
  call `prax cron install / uninstall / run`, surfacing the CLI's
  output in a small log box. Install writes a LaunchAgent on macOS or
  a crontab line on Linux; uninstall reverses it.
- **Notify wiring in form** — when adding a job, the notify-channel
  dropdown is populated from the channels saved in 0.2.0, so wiring
  "every day at 17:00 → push to my-wechat" is two clicks.
- **API contract**: `GET/PUT/DELETE /api/cron[/{name}]`,
  `POST /api/cron/{install,uninstall,run-once}`. Test send + dispatcher
  control all shell out to the `prax` CLI to avoid duplicating
  scheduler / LaunchAgent / crontab logic.

### Fixed
- **Channel-name placeholder UX gap** (0.2.0 surfaced this in beta):
  the placeholder text "my-wechat" looked like a real value, leading
  users to click Save with an empty name. The label now ends with a
  red `*`, and the placeholder explicitly says "这只是提示，请实际输入".

### Notes
- Tests: 21 unit tests (was 11). The new 10 cover cron CRUD + name
  validation + dispatcher shell-out arg shapes (subprocess is patched).

## [0.2.0] - 2026-04-25

### Added
- **Channels screen** — first real GUI: list / add / delete /
  test-send notification channels. Supports `wechat_personal`,
  `wechat_work_webhook`, `feishu_webhook`, `lark_webhook` (SMTP lands
  later). The form pulls saved iLink WeChat accounts (from
  `~/.prax/wechat/`) into a dropdown so users don't hand-copy long
  account_ids.
- **API contract** — `GET/PUT/DELETE /api/channels[/{name}]`,
  `POST /api/channels/{name}/test`, `GET /api/wechat/accounts`. Test
  send goes through `prax.tools.notify.build_provider` so what passes
  here also passes for `prax cron run`.
- **Reads/writes `<cwd>/.prax/notify.yaml`** in the same shape the
  `prax` CLI already expects — no migration, no shim, the GUI is
  literally a yaml editor with type-aware fields.

### Notes
- `~/.prax/wechat/` accounts are read-only here; QR login still goes
  through `prax wechat login` in the terminal (a one-time scan).
  Future work: wrap the QR flow in the GUI too (0.3.x).
- Tests: 11 unit tests cover the empty-list, upsert, delete, and
  mocked test-send paths.

## [0.1.0] - 2026-04-25

### Added
- Initial scaffolding. `praxdaily serve` starts a local FastAPI server
  on `127.0.0.1:7878`, opens the default browser, and renders a
  Vue+Tailwind shell that reports the runtime health (praxdaily version,
  cwd, whether `prax` is installed, whether `.prax/` exists in the cwd).
- `praxdaily run-now` shells out to `prax prompt "触发 ai-news-daily 技能"`
  with `--permission-mode workspace-write`. This is the same trigger
  the cron dispatcher will use; exposing it as a CLI command lets users
  smoke-test the chain before scheduling.
- Node wrapper `bin/praxdaily.js` mirrors `praxagent`'s pattern: looks
  for `python3` / `python` on PATH and shells into `python -m praxdaily`
  with the bundled `src/` on `PYTHONPATH`.

### Out of scope (planned for 0.2.x → 0.4.x)
- Five real screens: sources, schedule, channels, runs history, first-run setup.
- API contract for each screen (`POST /api/sources`, `POST /api/channels/test`,
  `GET /api/runs`, etc.) — to be specced separately.
- One-click "import the bundled ai-news-daily defaults" button.
- Beta loop with 5–10 non-developer users.
