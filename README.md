# praxdaily

> Self-hosted AI daily digest — a local web panel that turns the
> [Prax](https://github.com/ChanningLua/prax-agent) `ai-news-daily`
> skill into a one-screen, point-and-click setup. Hermes-equivalent
> workflow, runs entirely on your laptop.

---

## What you get

```
┌──────────────────────────────────────────────────┐
│ AutoCLI scrapes X / 知乎 / B 站 / HN             │  every day at <your time>
│   ↓                                              │
│ knowledge-compile turns it into Obsidian wiki    │
│   ↓                                              │
│ Push the digest to your personal WeChat          │  via prax wechat (iLink)
└──────────────────────────────────────────────────┘
```

All four steps already work in [praxagent](https://www.npmjs.com/package/praxagent)
0.5.x. **praxdaily is just the GUI** that lets a non-developer set it up
without touching `~/.prax/models.yaml` or `.prax/cron.yaml`.

## Install

```bash
# 1. install the runtime (Prax)
npm install -g praxagent

# 2. install this panel
npm install -g praxdaily

# 3. run it (opens http://127.0.0.1:7878 in your browser)
praxdaily serve
```

## Status

This is **0.1.0 — scaffolding only**. What's there:

| | status |
|---|---|
| `praxdaily serve` starts a local web server | ✅ |
| `/api/health` reports prax CLI / .prax/ presence | ✅ |
| Five real screens (sources / schedule / channels / runs / setup) | ❌ planned for 0.2.x |
| One-click ai-news-daily setup wizard | ❌ planned for 0.3.x |
| Beta with non-developer users | ❌ planned for 0.4.x |

If you're a developer, you can already do everything via the `prax` CLI
directly — see the [prax-agent README](https://github.com/ChanningLua/prax-agent).

## License

MIT
