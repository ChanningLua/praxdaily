# Changelog

All notable changes to praxdaily will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
