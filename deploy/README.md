# 部署指南

praxdaily 默认是单机本地（macOS LaunchAgent）跑法。这个目录给的是把它部署到 Linux 服务器、用 Docker + 反向代理 + HTTPS 的"公网展示"形态。

> **目标**：让朋友能扫一个 `https://daily.yourdomain.com` 的链接订阅；机器自己每天 14:00 抓 + 推；故障时通过飞书/钉钉群机器人告警。

## 0. 你需要

- 一台 Linux 服务器（任意云均可，最低 1C1G 够跑）
- 一个域名 + 解析到该服务器（A/AAAA 记录）
- 一个已登录的 iLink 账号凭证文件（`~/.prax/wechat/<account_id>.json`）

如果只是本地玩，跳过这个目录，看根 README 即可。

---

## 1. Docker 部署

```bash
# 在仓库根目录
mkdir -p workspace/.prax
# 把现有的 .prax 内容（sources.yaml / cron.yaml / notify.yaml / wechat/）拷过去
cp -r .prax/* workspace/.prax/

# 配置告警 webhook（可选）
export PRAXDAILY_ALERT_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/<token>"

docker compose up -d --build
docker compose logs -f praxdaily
```

容器内部署的两个服务：

- `praxdaily` — dashboard + bridge + iLink puller（暴露 :7878）
- `praxdaily-scheduler` — 每天 14:00 调用 `/api/cron/run-once`，触发原生 pipeline

`workspace/` 整个目录持久化（SQLite、digest 历史、日志全在里面），删容器不丢数据。

### 健康检查

```bash
curl -fsS http://127.0.0.1:7878/api/bridge/health | jq
# 期望:  status=ok, pullers_running >= 1
```

---

## 2. HTTPS + 公网入口（Caddy）

把 `Caddyfile.example` 拷成 `/etc/caddy/Caddyfile`，编辑：

1. `daily.example.com` → 你的真实域名
2. `admin@example.com` → 你的 ACME 邮箱
3. `basic_auth` 行的 hash → `caddy hash-password -p 'YourPassword'` 生成新的

启动：

```bash
caddy run --config /etc/caddy/Caddyfile
# 或 systemd: systemctl restart caddy
```

Caddy 会自动从 LetsEncrypt 申请证书，通常 10 秒内就绪。

> **为什么仍然建议加 basic auth**：dashboard 没有内建认证（这是个展示项目，对内场景不需要复杂账号体系）。Caddy basic_auth 是最便宜的"别让陌生人乱按按钮"防线。

---

## 3. 告警 webhook 配置

支持 **飞书 / 钉钉 / 企业微信** 自定义群机器人。把 webhook URL 设到 `PRAXDAILY_ALERT_WEBHOOK` 环境变量。三种事件会触发推送：

| 事件 | 触发条件 | 严重度 |
|---|---|---|
| `puller_error` | iLink getupdates 长轮询持续失败（已退避到 60s） | error |
| `send_failure` | 单用户广播失败（重试 3 次） | warn |
| `content_blocked` | 出站消息命中敏感词被拦 | error |

5 分钟同 key 去重，不会刷屏。

### 测试

```bash
# 把 webhook 设成你自己的，发一条测试消息
PRAXDAILY_ALERT_WEBHOOK="https://open.feishu.cn/..." \
python3 -c "
import asyncio
import sys; sys.path.insert(0, 'src')
from praxdaily.bridge import alerts
asyncio.run(alerts.send_alert(kind='test', message='hello from praxdaily', severity='info'))
"
```

应在群里看到 "ℹ praxdaily/test\nhello from praxdaily"。

---

## 4. 内容审核词库

默认词库内置在 `bridge/moderation.py`（短，覆盖最容易触发风控的几类）。要扩展自定义：

```yaml
# workspace/.prax/sensitive_words.yaml
mode: extend     # 'extend' = 默认 + 自定义；'replace' = 只用自定义
words:
  - 自定义词1
  - 自定义词2
```

命中即整批广播 abort + 告警 + 落 messages.status='blocked'。

---

## 5. 故障排查

### Puller 一直在 SSL_VERIFY_FAILED

通常是宿主机走了透明代理 / 公司网络中间人 TLS。在容器里跑 一般不会撞。

### 推送 ret=-2

iLink 需要 user 先给 bot 发过任意消息建立会话上下文。让用户在微信里给 bot 发个 `hi`，下次 cron 就能推到了。

### 朋友扫码后提示 "登录某小程序"

正常——这是 iLink 协议复用 bot 登录 QR 的副作用。文案上你可以告诉用户"看到登录提示直接同意"。

### 容器重启后 puller 没起来

`docker compose logs praxdaily | grep PullerManager` 看启动日志。常见原因：iLink 凭证文件没挂进容器（看 `docker-compose.yml` 注释那两行 volume）。

---

## 6. 这是展示项目，不是商用

这套部署方案有意省略了若干商用必要件：

- ❌ 没有 dashboard 用户体系（只有 Caddy basic auth 一道闸）
- ❌ 没有支付集成
- ❌ 没有号池 / 自动 failover（单 wxid 封号 = 全停服）
- ❌ 没有 SLA 监控仪表板
- ❌ 没有合规文本（用户协议 / 隐私政策 / PIPL consent）

要把它变成商用产品，参考根目录 plan 文件中的"必做"清单。
