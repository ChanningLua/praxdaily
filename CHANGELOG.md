# Changelog

All notable changes to praxdaily will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.1] - 2026-04-28

Polish pass — everything that would have made a fresh reader stop and
go "wait what?" in the 0.7.0 walkthrough is now handled.

### Added

- **「最近一份日报」preview card on the overview tab** — reads the most
  recent `<workspace>/.prax/vault/<date>/daily-digest.md` and renders
  it inline. Auto-refreshes after every 立即触发. Solves the "did
  WeChat actually receive what I think I pushed?" question without
  making the user open WeChat or `cat` files.
- **`GET /api/runs/latest-digest`** endpoint backing the above.
  Picks the newest date directory; gracefully skips empty / failed
  ones; defends against the `/{filename}` route shadowing it.
- **Friendly fix-step UI when push fails with iLink `ret=-2`** —
  pipeline now classifies the error and returns
  `{hint_kind, hint_title, hint_steps}`. The trigger-result panel
  shows the steps as an ordered list inside an orange callout instead
  of the raw `RuntimeError` traceback. Same treatment for
  "wechat account not registered" errors.

### Changed

- **Setup checklist on overview rewritten** to match 0.7's
  architecture: (1) install praxagent CLI, (2) login WeChat,
  (3) configure channel, (4) install daily schedule. Each unchecked
  item now has a "去这步 →" jump button. The success card now points
  users at the cron tab's 立即触发 button as the end-to-end smoke test.
- **Trigger-now's success/fail headline reflects notify reality** —
  exit-code 0 with `notify.sent: false` no longer reads as a green
  success. Headline changes to "⚠ 跑完了但推送失败：<hint title>"
  when known fixable, "✗ 退出 N" otherwise.
- **Cron tab subtitle + buttons rewritten** for the new
  praxdaily-owned schedule. The leftover "跑所有到期" / cronOutput pre /
  the install/uninstall buttons that drove the old prax cron
  dispatcher are gone (or replaced).

### Tests

178 unit tests pass (10 new for latest-digest + 5 for the error
classifier). 6 e2e gated as before.

## [0.7.0] - 2026-04-27

### Architecture rewrite — daily-digest is now native, no LLM in the data path

Pre-0.7, scheduled runs and "立即触发" both shelled out to `prax prompt`
which had the LLM follow `ai-news-daily` SKILL.md instructions —
including running AutoCLI / tmux for scraping. That path was flaky:
AutoCLI is a third-party tool with Chrome-extension dependencies, the
LLM hit circuit breakers, and on rare occasions the LLM would
recursively re-invoke `praxdaily run-now` itself (infinite loop).
Real end-to-end success rate was effectively zero.

0.7 replaces the data path with deterministic Python:

- **Native scrapers** (`src/praxdaily/scrapers/{hn,bilibili}.py`) —
  pure httpx + JSON, no external CLI, no Chrome extension. HackerNews
  via the public Firebase API. B 站 via the popular API (off by
  default — its content is heavily gaming/anime which fails AI
  keyword filtering).
- **`pipeline.run()`** — scrape → keyword filter → top-N by metric
  → render markdown → push notify. Async-aware so the FastAPI route
  awaits it directly.
- **`praxdaily-owned LaunchAgent`** (`src/praxdaily/scheduler.py`) —
  installs `~/Library/LaunchAgents/com.praxdaily.daily.plist` that
  invokes `praxdaily run-now` on schedule. Both manual and scheduled
  runs share the same code path → behaviour is consistent.

Manual trigger (`POST /api/cron/{name}/trigger-now`) auto-detects
"ai-news-daily"-style prompts and routes to the native pipeline;
any other prompt still shells out to prax for back-compat.

### Added

- **`praxdaily install-schedule [--time HH:MM]`** / `uninstall-schedule`
  / `schedule-status` CLI commands.
- **`/api/schedule`** routes — same operations as the CLI, plus
  legacy `prax cron` dispatcher detection
  (`POST /api/schedule/uninstall-legacy-prax-cron`).
- **GUI cron tab** got a "每日定时" card replacing the prax-cron-based
  install/uninstall buttons. Auto-detects the legacy
  `dev.prax.cron.dispatcher` plist and offers a "清理旧调度器" button
  so users running both versions don't get duplicate triggers.
- **Settings tab (⌘7)** — three friendly cards for OpenAI / Anthropic
  / GLM API keys + per-provider `base_url` override (for relays).
  Writes to `<cwd>/.prax/.env` and `~/.prax/models.yaml`. Detects
  and warns when a workspace-level `.prax/models.yaml` shadows
  user-level config (the `/init-models` ghost-file footgun fixed in
  praxagent 0.5.5 but still surfaced here for older installs).
  "测一下" button probes the relay's `/models` and falls back to
  `/chat/completions` to catch URL typos (missing `/v1`, etc.).
- **Channel form rewrite** — visual provider cards (📱 我的微信 /
  💼 企业微信群 / 🚀 飞书 / 🌍 Lark) with auto-suggested channel
  names and "advanced options" collapsed by default. Replaces the
  previous dev-style `provider/account_id/to/title 前缀` form that
  non-technical users couldn't parse.
- **Trigger-now sends notify on success/failure** matching the cron
  dispatcher behaviour, so "立即触发" actually validates the whole
  push chain end-to-end.
- **Per-job `model` field on cron** — pin a job to a specific model
  (e.g. `gpt-5.4`) to avoid prax's tier-routing escalating to a
  model that's missing credentials. Surfaces in the cron form.
- **Multi-message wechat send with retry** — long digests are split
  into chunks (per source), sent ~2s apart with up to 3 retries on
  iLink `ret=-2` ("session context lost") errors.

### Changed

- **`praxdaily run-now`** uses `pipeline.run()` directly. No more
  `prax prompt` shell-out. Exits 0 on full success, 1 on push
  failure, 2 on pre-scrape fatal error.
- **Default sources** — only HackerNews enabled by default. Twitter,
  Zhihu, Bilibili are disabled because they need login state
  (X / 知乎) or have a too gaming-heavy popular feed (B 站).
  Schema kept so they can be enabled when scrapers ship later.
- **Default keywords tightened** — removed `推理` / `agent` /
  `智能体` (matched gaming/anime contexts) in favour of precise
  terms like `大模型 / Anthropic / OpenAI / RAG / transformer`.
  Short ASCII keywords like `AI` / `AGI` now match at word
  boundaries (fixes `AGI` → `MAGIC` substring false-positives).
- **HN min_metric default = 100** — only stories with ≥100 upvotes
  count as "today's hot". Configurable per-source in `sources.yaml`.

### Migration

If you set up praxdaily before 0.7.0, open the GUI cron tab — it
will show an orange warning banner if the legacy
`dev.prax.cron.dispatcher` plist is installed. Click
**清理旧调度器** then **安装定时** in the "每日定时" card. Then
**立即触发** to verify end-to-end.

CLI equivalent:

```bash
launchctl unload ~/Library/LaunchAgents/dev.prax.cron.dispatcher.plist
rm ~/Library/LaunchAgents/dev.prax.cron.dispatcher.plist
praxdaily install-schedule --time 14:00 --cwd ~/your-project
```

## [0.6.2] - 2026-04-25

### Polish (3 product-feel improvements, no functional change)
- **Page header in main area** — every tab now opens with a 24px H1
  title + muted subtitle ("Manage .prax/notify.yaml — 微信/飞书/邮件
  等推送目标" etc.) above the KPI strip. Saves users from hunting for
  "where am I" inside a section card and gives the panel an editorial
  rhythm. Subtitles live in the same `tabs` array so adding a new
  tab in the future updates the header automatically.
- **Toast notifications** (bottom-right, glass styled) replace four
  scattered inline message places (channel test result, channel save
  error, sources save success, cron trigger result). Auto-dismiss at
  4s for info/success and 6s for errors; `×` button for manual
  dismiss. Animates in from the right with `@keyframes toastIn`. Border
  color reflects kind (success → ok / error → danger / info → acc).
  Side benefit: forms shrink because they no longer carry status
  spans below the submit button.
- **Keyboard shortcuts**:
  - `⌘1`–`⌘6` (or `Ctrl+1`–`6`) jumps to the corresponding tab —
    the chord is shown as a small `<kbd>` chip on each sidebar item
    and the page header carries a `⌘K → 切换 tab` hint to teach the
    pattern at a glance.
  - `Esc` closes the run-log modal.
  Implemented as a single `keydown` listener registered in `onMounted`,
  so it lives inside Vue's lifecycle and won't leak across hot reloads.

### Notes
- All 42 unit tests still pass — pure presentation.
- HTML grew to ~57KB (was 53KB after 0.6.1). Still single file, no
  external JS bundle.

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
