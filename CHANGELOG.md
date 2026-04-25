# Changelog

All notable changes to praxdaily will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.1] - 2026-04-25

### Polish (5 visual micro-improvements, no functional change)
- **Card hover lift** — glass cards now translate up 2px with stronger
  shadow on hover, mirroring openclaw-manager's `.edict-card:hover`.
  Applied to all four KPI cards via the new `.glass-hover` class.
- **KPI icons** — each top-strip card now leads with a colour-tinted
  rounded square holding a 16px lucide icon (terminal / chat / mail /
  clock). Visual anchor + faster scanning.
- **Empty states with icon + CTA** — replaces "还没有 X" plain text in
  4 places (no WeChat accounts / no channels / no cron jobs / no
  runs). 40px outline icon at 30% opacity, two-line title + hint, CTA
  noun is highlighted in accent blue. Runs-empty state's CTA word is
  click-to-jump to the cron tab.
- **Tab switch fade-in** — every section now plays a 0.22s
  fade-up-from-8px animation when its tab becomes active
  (`@keyframes tabIn`). Implemented as a CSS class triggered by Vue's
  v-if remount, so it plays exactly once per switch.
- **Custom scrollbar + focus ring** — scrollbar thumb is a translucent
  blue pill (`rgba(140,180,230,0.18)`) instead of the default OS grey;
  keyboard `:focus-visible` on buttons gets a 2px accent outline with
  2px offset for accessibility without touching mouse-click visuals.

### Notes
- All 42 unit tests still pass — pure presentation-layer change.
- Tracked the recent fad of "skeleton loaders" but didn't add them:
  the API endpoints respond in single-digit ms, so the existing "加载…"
  text disappears before a skeleton would even paint.

## [0.6.0] - 2026-04-25

### Changed (UI overhaul — 借鉴 openclaw-manager 设计语言)
- **Dark glass-morphism shell** — page background is now deep blue-black
  (#04050a) with two subtle radial-gradient auroras and a fixed
  attachment, matching the production-grade feel of openclaw-manager's
  edict frontend. Cards use `backdrop-filter: blur(14px) saturate(140%)`
  with a 1px translucent border and layered shadow. CSS variables in
  `:root` (`--bg`, `--panel`, `--ok`, `--warn`, `--danger`, `--acc`,
  `--acc2`, `--radius-*`) drive the whole palette so theme tweaks live
  in one place.
- **Left sidebar navigation** — six tabs (概览 / 微信账号 / 通知渠道 /
  抓取源 / 定时任务 / 运行历史) with monochrome lucide-style SVG icons
  and live count badges. Replaces the previous "scroll through 7
  stacked sections" layout. Active tab gets a blue→cyan gradient fill
  with a stronger border.
- **Top KPI strip** (always visible, 4 cards) — prax CLI version /
  WeChat accounts / channel count / cron-job count, each tinted with
  its own gradient background (blue/cyan/green/amber). One-glance
  health.
- **Status pills** with semantic color tokens (`pill-ok / pill-warn /
  pill-danger / pill-acc / pill-muted`) replace plain text in tables
  and detail panels — runs history, cron notify config, channel
  provider all now visually distinct.
- **Pulse animation** on pending run-status indicators while
  background fetches resolve, mirroring openclaw's loading affordances.
- **Scoped form styling** — `<input>` / `<select>` / `<textarea>` are
  globally restyled to match the dark theme: translucent background,
  brand accent on focus, 10px radius, `accent-color` for checkboxes.
- **Modal viewer** for cron logs is now a full glass overlay with
  backdrop blur, dark monospace `<pre>`, and a clean ✕ in the header.

### Kept on purpose
- Vue 3 + Tailwind via CDN — zero build, single HTML file. The
  openclaw-manager 3D dispatch room and React/TypeScript/Vite
  toolchain were intentionally NOT borrowed; they're overkill for a
  local-tool panel.
- All API contracts unchanged. The 5 yaml-editor screens and their
  routes are identical to 0.5.0; only the presentation layer was
  rewritten.

### Notes
- HTML grew from ~28KB to ~48KB (the dark theme + sidebar + tab
  router add real content); still a single file with no external JS
  bundle.
- All 42 unit tests still pass — UI-only change.

## [0.5.0] - 2026-04-25

### Added
- **Sources screen** — manage which platforms `ai-news-daily` scrapes,
  per-source `limit` / `top_n`, and the keyword include/exclude
  filters. Writes to `<cwd>/.prax/sources.yaml`. Pairs with the
  contract that landed in praxagent **0.5.4** — the skill loads this
  file (or its DEFAULTS) at Step 1.5.
- **API contract**: `GET / PUT / DELETE /api/sources`. GET layers
  user values over DEFAULTS so the table always shows the full shape
  even when no file exists. PUT validates `top_n ≤ limit`, rejects
  duplicate ids, strips blank tag strings. DELETE removes the file
  so the skill falls back to baked-in defaults.
- **Forward compat**: unknown source ids (e.g. `weibo` if the user
  adds one) are preserved through write/read with a small "skill
  暂不映射此 id, 会跳过" hint in the UI.

### Required runtime
Bumps the praxagent peer requirement to `>=0.5.4` because that's
where the SKILL.md contract lives.

### Notes
- Tests: 42 unit (was 34). +8 cover GET defaults, PUT yaml shape,
  layered GET-after-PUT, duplicate-id reject, top_n>limit reject,
  blank-tag stripping, DELETE reset, and unknown-id preservation.
- This closes the original 5-screen plan. praxdaily 0.5.x is now
  feature-complete for the Phase 1 scope; next milestone is Phase 2
  (招 5-10 个 beta 用户) and Phase 3 (公众号 / 小红书 案例化传播).

## [0.4.1] - 2026-04-25

### Fixed (UX gaps from self-test)
- **"立刻跑一次" only fired DUE jobs** — at any time other than the
  scheduled minute the user got `No due jobs at <ts>` and no clue why
  their newly-added 17:00 job didn't run. New per-row **"立即触发"**
  button on each cron job force-runs that one job's prompt now,
  bypassing the schedule check. Goes through `prax prompt <prompt>
  --permission-mode workspace-write` (same code path as the cron
  dispatcher's subprocess). Result + tail surface back into the GUI
  in a green/red box. (`POST /api/cron/{name}/trigger-now`)
- **Cron jobs could be saved with a notify_channel that doesn't
  exist** — the job ran but silently no-op'd the notify step at
  runtime, leaving users wondering why their phone never buzzed.
  Upsert now validates the channel exists in `.prax/notify.yaml` and
  returns a 400 with a clear message if not.
- **`.prax/ exists` health row was stale after CRUD** — adding the
  first channel/cron creates `.prax/`, but the top status panel
  still said `no` until reload. Each save now re-fetches health.
- **wechat_personal account_id dropdown was empty + silent** when no
  iLink accounts were logged in — users had no idea what to do. The
  dropdown is now disabled in that state with an inline orange hint
  pointing to `prax wechat login`.

### Notes
- Tests: 34 unit (was 29). +5 cover trigger-now happy path, 404 on
  missing job, 503 on missing CLI, channel-validation reject, and
  empty-notify_channel acceptance. The previous "save cron with
  channel" test was updated to PUT the channel first.

## [0.4.0] - 2026-04-25

### Added
- **Runs history screen** — table of all `.prax/logs/cron/*.log` newest
  first, click a row to open a modal with the full log content. Status
  (`success` / `failure`) is inferred from log markers (LLM connection
  errors, Python tracebacks, etc.) so users get a quick visual signal
  without opening every file.
- **Quick Setup checklist** — top of the panel shows a 4-step
  onboarding ribbon (`✓` 装 prax / 登微信 / 配渠道 / 加任务), tracked
  live from the existing API state. Once all four turn green, the
  ribbon prompts to hit "立刻跑一次".
- **API contract**: `GET /api/runs` (newest-first listing) and
  `GET /api/runs/{filename}` (content + inferred status). Path
  traversal blocked at three layers — syntactic check, resolved-path
  containment, and FastAPI's URL routing.

### Notes
- This closes the GUI scope set in the original plan (5 screens:
  Health / WeChat accounts / Channels / Schedule / Runs). Sources is
  the one remaining gap, deferred to 0.5.0 because it requires
  changing the `ai-news-daily` skill's prompt contract to read sources
  from a config file (today they're hardcoded in SKILL.md).
- Tests: 29 unit tests (was 21). +8 cover runs listing,
  newest-first ordering by parsed timestamp, status inference,
  404-on-missing, and three different path-traversal payloads.
- Phase 2 unblocked: with 5 screens shipped, beta recruitment can
  start. Recommended cohort: 5-10 non-developers (PM / 自媒体 /
  学生), 2-week loop, capture install_time / first_run_time /
  drop_off_step / wow_moment per user.

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
