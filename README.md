# praxdaily

> Self-hosted AI 信息助理 —— 本地网页面板，每天定时把 HackerNews 的 AI
> 热门拉到你自己的微信。完全本地运行，零 SaaS 依赖。

[![npm version](https://img.shields.io/npm/v/praxdaily.svg)](https://www.npmjs.com/package/praxdaily)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 这是什么

一个本地起的网页面板（`http://127.0.0.1:7878`），帮你做这件事：

```
   每天定时
     ↓
  HackerNews 公开 API → 抓 30 条 → AI 关键词过滤 → 取最热的 5 条
     ↓
  按你 sources.yaml 的设置渲染成 markdown 日报
     ↓
  推到你登录的个人微信（通过 iLink Bot）
```

**没有** LLM 调用、**没有** 浏览器扩展、**没有** API key 强制要求（数据抓取
走公开端点）。完全是确定性的 HTTP 流程。

> 想要 X / 知乎 / B 站？目前只内置 HackerNews scraper。其他源等后续版本（见
> [Roadmap](#roadmap)）。

---

## 5 分钟跑通

### 0. 前置

- macOS（Linux 的 LaunchAgent 等价机制还没实现）
- Python 3.10+
- Node.js + npm（装 npm 包用）
- 一个微信账号

### 1. 安装

```bash
npm install -g praxagent praxdaily
```

两个包都装：`praxagent` 提供 `prax wechat` 命令（用来登录微信），`praxdaily`
是面板本身。

### 2. 启动

```bash
cd ~/some-project   # 配置都存在这个目录的 .prax/ 下
praxdaily serve
```

浏览器自动打开 `http://127.0.0.1:7878`。

### 3. 在面板里走 4 步

按左边栏 ⌘2 → ⌘3 → ⌘5 顺着走：

**① 微信账号 (⌘2)** —— 点「+ 登录新账号」→ 微信扫码 →
账号会出现在表里。这是日报推送的目的地。

**② 通知渠道 (⌘3)** —— 点「+ 新增渠道」→
1. 推到哪？选 **📱 我的微信**（已经推荐选中）
2. 选刚登录的微信账号
3. 名字可以接受默认（如 `wechat-xxxxxx`）
→ 保存。

**③ 定时任务 (⌘5)** —— 顶部「每日定时」卡片：
1. 时间填 `14:00`（或你想要的）
2. 点「安装定时」 → 显示绿色「已安装 · 每天 14:00 触发」

如果有橙色警告条说"检测到旧的 prax cron 调度器"，先点「清理旧调度器」
再装新的。

→ 然后点「+ 新增任务」加一个 cron 任务名（任意，比如 `daily-news`），
schedule 选每天某时，prompt 留默认 `触发 ai-news-daily 技能`，渠道选刚才
建好的那个。保存。

### 4. 测试一下

回「定时任务」屏，找到刚加的任务，点最右边「**立即触发**」。等 5-10 秒。

如果一切正常：
- 浏览器里看到绿色「触发完成 (exit 0) · notify sent: true」
- 微信里收到 3-5 条消息：标题、HN 列表、结尾签名

如果没收到看 [常见问题](#常见问题)。

---

## 你会收到什么

类似这样的消息（实际内容看当天的 HN 热门 + 你的关键词过滤）：

```
📅 AI 日报 · 2026-04-27

今日 4 条：
  📰 HackerNews · 4 条

👇 详细内容看下面几条
```

```
📰 HackerNews
———————
1. An AI agent deleted our production database. The agent's confession is below
   by jeremyccrane · 🔥 550 分
   🔗 https://twitter.com/lifeof_jer/...

2. AI should elevate your thinking, not replace it
   by koshyjohn · 🔥 337 分
   🔗 https://www.koshyjohn.com/blog/...
```

```
——— 共 4 条 · praxdaily 自动生成 ———
```

每天的 markdown 完整版会归档在 `<workspace>/.prax/vault/<YYYY-MM-DD>/daily-digest.md`。

---

## 自定义

### 改触发时间

定时任务屏 → 时间框改成 `09:30` 之类 → 点「更新时间」。LaunchAgent 会
重新加载。

### 改抓取量 / 关键词

抓取源屏（⌘4）：

- **HackerNews** 默认抓 30 条 → 关键词过滤 → 取分数最高的 5 条且 ≥100 分
- **关键词列表**：精确的 AI 词如 `AI / GPT / LLM / 大模型 / RAG / Anthropic / OpenAI / transformer` 等。**不要**加宽词如 `agent` / `推理` / `智能`，会命中游戏 / 动漫内容
- 改完点保存

底层文件是 `<workspace>/.prax/sources.yaml`，命令行用户可以直接编辑。

### 同时管理多个项目

侧栏左上角的工作目录下拉 → 「+ 添加目录」→ 填绝对路径。每个目录独立的
`.prax/` 配置，互不干扰。

### LLM API Key（可选）

设置屏（⌘7）—— **日报本身不用 LLM**，所以这屏对纯日报用户**完全可选**。
如果你想给其他 cron job 接 LLM（praxagent 直接的能力），在这里配 OpenAI /
Anthropic / GLM 的 key + 中转站 base_url。配完点「测一下」按钮即时验证。

---

## 常见问题

### 「立即触发」exit 0 但微信没收到

99% 是 iLink session 上下文掉了。**修复**：

1. 微信里找到 bot 联系人（之前推送的来源）
2. 给它发一句任意内容（比如 `ping`）
3. 回 praxdaily 再点「立即触发」

如果还不行，看「运行历史」屏的最新一条记录的 tail，里面会有具体错误。

### 「立即触发」直接报错 / output 是空的

通常是 cwd 错了。看 health bar 上的 `cwd` 字段是不是你预期的工作目录。
不对的话切到正确的工作目录（侧栏下拉），或者重新 `praxdaily serve --cwd /path/to/your/project`。

### 14:00 到点没自动跑

```bash
launchctl list | grep praxdaily          # 看 com.praxdaily.daily 在不在
cat ~/Library/LaunchAgents/com.praxdaily.daily.plist     # 检查时间字段
```

也可以直接 `praxdaily schedule-status` 看完整状态。

### 想改默认时间

```bash
praxdaily install-schedule --time 09:00 --cwd ~/your-project
```

会覆盖之前的。

### 卸载

```bash
praxdaily uninstall-schedule
npm uninstall -g praxdaily praxagent
```

`<workspace>/.prax/` 不会自动删，里面有你的 yaml 配置和历史 vault，需要手动清。

---

## 命令行（不想用 GUI 也行）

```bash
praxdaily serve                                  # 启动面板
praxdaily run-now [--cwd .]                      # 立刻跑一次
praxdaily install-schedule --time 14:00          # 装每日定时
praxdaily uninstall-schedule                     # 卸载
praxdaily schedule-status                        # 看当前状态
```

---

## 数据流向

```
~/.praxdaily/workspaces.json         所有工作目录的注册表
  └── 每个 workspace 下面：
      .prax/notify.yaml              通知渠道（微信账号 / 飞书 webhook 等）
      .prax/sources.yaml             抓取源 + 关键词
      .prax/cron.yaml                cron 任务定义（schedule + prompt + 渠道）
      .prax/.env                     LLM API key（chmod 600，可选）
      .prax/vault/<date>/daily-digest.md   每日产出归档
      .prax/logs/schedule/*.log      LaunchAgent 跑的 stdout/stderr

~/Library/LaunchAgents/com.praxdaily.daily.plist     macOS 定时任务（praxdaily 自己的）
~/.prax/wechat/accounts.json         iLink 微信账号 token（praxagent 管）
```

数据**不离开你的电脑**。HackerNews 抓取走 `https://hacker-news.firebaseio.com/`（公开 API），
微信推送走 `iLink Bot`（你登录的账号自己发给自己）。

---

## Roadmap

- [x] 0.7 · native pipeline（去 LLM / 去 AutoCLI 依赖）+ 自管 LaunchAgent
- [ ] 0.8 · 知乎 / X 抓取（走 RSSHub 公开实例）
- [ ] 0.9 · Linux crontab 支持
- [ ] 1.0 · LLM 总结模式（可选）—— 让 LLM 把抓的多条整理成"今日要点"段落

---

## 开发

```bash
git clone https://github.com/ChanningLua/praxdaily
cd praxdaily
pip install -e ".[dev]"
PYTHONPATH=src python3 -m pytest -q   # 100+ 测试
PYTHONPATH=src python3 -m praxdaily serve --no-open --port 7878
```

---

## License

MIT
