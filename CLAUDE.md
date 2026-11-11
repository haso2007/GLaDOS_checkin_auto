# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

此文件为 Claude Code (claude.ai/code) 在本仓库中工作提供指导。

---

## Project Overview / 项目概述

GLaDOS Auto Check-in - Automated daily check-in script for GLaDOS VPN service. Performs automatic check-ins to maintain VPN service days and automatically exchanges points for the maximum possible days when account is on its last day. Aligned with the latest GLaDOS web frontend checkin protocol (challenge & proof verification, device adaptation, and multi-domain probing).

GLaDOS 自动签到 - GLaDOS VPN 服务的自动每日签到脚本。自动执行签到以维持 VPN 服务天数，并在账户剩余最后一天时自动用积分兑换最大可用天数。与网页端最新签到机制完全对齐（支持 challenge & proof 校验、设备自适应 UA、多域名自动探测）。

## Running the Script / 运行脚本

```bash
# Main script (GitHub Actions & CLI execution)
# 主脚本（GitHub Actions 与本地命令行运行）
python3 glados.py
```

**Required dependency / 必需依赖:** `pip install -r requirements.txt` (`requests>=2.31,<3`)

## Environment Variables / 环境变量

| Variable / 变量 | Required / 必需 | Description / 描述 |
|----------------|-----------------|-------------------|
| `GLADOS_COOKIES` / `GLADOS_COOKIE` | ✅ | JSON array format (recommended) or legacy `&` separated string / JSON 数组格式（推荐）或旧版 `&` 分隔 |
| `AUTO_EXCHANGE` | ❌ | Enable/disable auto-exchange on expiry (default: true) / 账户即将到期时自动兑换（默认启用） |
| `AUTO_EXCHANGE_LEFT_DAYS` | ❌ | Remaining days threshold to trigger auto-exchange (default: 1) / 触发自动兑换的剩余天数阈值（默认 1 天） |
| `GLADOS_BASE_URLS` | ❌ | Custom base URLs (comma/semicolon separated) / 自定义基础 URL 列表 |
| `GLADOS_VERBOSE` | ❌ | Verbose output (`true` / `1` / `yes`) / 详细日志输出 |

## Cookie Format / Cookie 格式

**Recommended JSON array / 推荐 JSON 数组格式:**
```json
[
  {
    "name": "主号",
    "origin": "glados.cloud",
    "cookie": "koa:sess=...; koa:sess.sig=...; gld:sess=...",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ..."
  }
]
```

- Allowed keys: `name` (optional), `origin` (optional), `cookie` (required), `user-agent` (optional).
- `cookie` must contain session fields: `koa:sess=` or `gld:sess=`.
- Legacy `&` separation is supported for backward compatibility: `cookie1&cookie2`.

## Architecture / 架构

**Execution flow / 执行流程:**
1. Load cookies & configuration from environment variables / 从环境变量加载配置与账号 Cookie
2. Multi-domain probing (`glados.cloud`, `railgun.info`, `glados.network`, `glados.rocks`, etc.) to find logged-in origin / 多域名探测以锁定有效域名
3. Fetch checkin challenge (`/api/user/checkin/challenge`), wait 2.5s, calculate sha256 proof (`{nonce}.glados-observe-v1`) / 获取 challenge 并计算 proof
4. Submit checkin POST with token, nonce, and proof. If device mismatch occurs, adapt User-Agent to match login device / 提交签到请求；如遇设备不匹配自动自适应 UA 重试
5. Query status and points / 查询账户状态与积分余额
6. Execute auto-exchange when remaining days <= threshold (default 1 day), redeeming the maximum days possible based on current points / 剩余最后一天时自动兑换最大可用天数

**GLaDOS API endpoints / API 端点:**
- `/api/user/checkin/challenge` - Checkin challenge (GET) / 签到 challenge 验证
- `/api/user/checkin` - Daily check-in (POST) / 每日签到
- `/api/user/status` - Account status & leftDays (GET) / 账户状态
- `/api/user/points` - Points balance (GET) / 积分余额
- `/api/user/exchange` - Exchange points for days (POST) / 积分兑换天数

**Auto-Exchange tiers (on last day) / 自动兑换档位（剩余最后一天时兑换最大天数）:**
| Points / 积分 | Days / 天数 | planType |
|--------------|------------|----------|
| 500+ | 100 | plan500 |
| 200+ | 30 | plan200 |
| 100+ | 10 | plan100 |

## GitHub Actions

Workflow: `.github/workflows/runGladosAction.yml`
- Scheduled daily at 17:00 UTC / 每天 UTC 17:00 执行（北京时间凌晨 1:00）
- Manual trigger via workflow_dispatch / 支持手动触发
- Triggers on repository star / Star 仓库时触发
- Uses repository secrets: `GLADOS_COOKIES`, `GLADOS_COOKIE`, `AUTO_EXCHANGE`, `AUTO_EXCHANGE_LEFT_DAYS`, and `GLADOS_VERBOSE`.
