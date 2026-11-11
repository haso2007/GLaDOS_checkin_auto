# GLaDOS 自动签到

全自动每日签到脚本，与 GLaDOS 网页端最新签到机制完全对齐（支持 challenge/proof 验证、设备平台自适应、候选域名自动探测与最后一天按积分自动兑换最大天数）。

支持 **GitHub Actions** 部署。

---

## 环境变量说明

| 环境变量 | 必填 | 说明 |
| --- | :---: | --- |
| `GLADOS_COOKIES` / `GLADOS_COOKIE` | **是** | 账号 Cookie 配置，支持 JSON 数组（推荐）或旧版 `&` 分隔 |
| `AUTO_EXCHANGE` | 否 | 账户在最后一天时自动用积分兑换最大可用天数（`true`/`1` 开启，默认开启） |
| `AUTO_EXCHANGE_LEFT_DAYS` | 否 | 触发自动兑换的剩余天数阈值（默认 `1` 天，即 0 或 1 天时兑换） |
| `GLADOS_BASE_URLS` | 否 | 自定义站点域名列表（逗号或分号分隔） |
| `GLADOS_VERBOSE` | 否 | 是否输出详细日志（`true` / `1`） |

---

## Cookie 格式配置

### 推荐：JSON 数组格式

在 GitHub 仓库 Secrets 中配置 `GLADOS_COOKIES`（或 `GLADOS_COOKIE`）：

```json
[
  {
    "name": "主号",
    "origin": "glados.cloud",
    "cookie": "koa:sess=...; koa:sess.sig=...; gld:sess=...",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ..."
  },
  {
    "name": "小号",
    "origin": "railgun.info",
    "cookie": "koa:sess=...; koa:sess.sig=..."
  },
  {
    "cookie": "gld:sess=..."
  }
]
```

- **允许字段**：`name`（备注名，可选）、`origin`（站点域名，可选，不填自动探测）、`cookie`（完整 Cookie，必填）、`user-agent`（提取 Cookie 时的浏览器 UA，可选，建议填写）。
- **Cookie 要求**：必须包含登录会话字段（`koa:sess=` 或 `gld:sess=`）。
- **重复去重**：同一 Cookie 自动去重，避免重复签到。

### 兼容格式：`&` 分隔

旧版格式 `cookie1&cookie2` 仍完全兼容支持，脚本运行时会自动识别并提示建议迁移为 JSON 数组。

---

## 签到与自动兑换机制

1. **网页端协议对齐**：先请求 `/api/user/checkin/challenge` 获取动态 `nonce`，等待 2.5 秒后计算 SHA256 签名 `proof`，携带 `token`、`nonce`、`proof` 发起签到，确保获得完整签到奖励。
2. **设备 UA 自适应**：如服务端返回 `device-mismatch` 错误，脚本会自动识别登录设备平台（Windows / macOS / Linux / iOS / Android）并自适应切换 User-Agent 重试。
3. **域名自动探测**：内置候选域名池（`glados.cloud`, `railgun.info`, `glados.network`, `glados.rocks`, `glados.one`, `glados.space`, `glados.vip`, `glados-facility.com`），自动识别 Cookie 对应的登录站点。
4. **最后一天自动兑换最大天数**：延续原项目核心逻辑，当账户剩余天数进入最后一天（剩余天数 <= 1 天）时，自动根据当前账户积分余额兑换最大可用档位：
   - 500+ 积分：兑换 100 天 (`plan500`, 500->100 days)
   - 200+ 积分：兑换 30 天 (`plan200`, 200->30 days)
   - 100+ 积分：兑换 10 天 (`plan100`, 100->10 days)
   - 低于 100 积分：日志提示当前积分与差额，等待积分满足。

---

## GitHub Actions 部署

1. Fork 本仓库。
2. 在 Fork 的仓库进入 `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`。
3. 添加 `GLADOS_COOKIES`（填入上述 JSON 数组或 Cookie 内容）。
4. 进入 `Actions` 标签页，启用工作流。
5. 可以手动触发一次 `workflow_dispatch` 检验运行状态，每天定时自动执行。
