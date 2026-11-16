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

- **允许字段**：`name`（备注名，可选，建议填邮箱以便日志直接显示账号名）、`origin`（站点域名，可选，不填自动探测）、`cookie`（完整 Cookie，必填）、`user-agent`（提取 Cookie 时的浏览器 UA，可选，建议填写）。
- **Cookie 要求**：必须包含登录会话字段（`koa:sess=` 或 `gld:sess=`）。
- **重复去重**：同一 Cookie 自动去重，避免重复签到。

把已有 Cookie 写进本机 Edge 或 Chrome、打开已登录控制台的步骤见 [BROWSER_COOKIE_LOGIN.md](BROWSER_COOKIE_LOGIN.md)。文档包含 Cookie-Editor 导入和开发者工具逐条手写两种做法。

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

## 容器浏览器签到（推荐）

GLaDOS 的签到页面如被 Cloudflare 人机验证拦截，GitHub Actions 的纯 HTTP 请求无法完成验证。此仓库提供一个本地 Docker 服务：每个账号运行在独立的 Camoufox 浏览器 profile 中，并使用独立或轮换代理。Cloudflare 验证在真实浏览器内完成，`cf_clearance` 不会导出到 Python HTTP 客户端。

### 启动

```bash
docker compose up -d --build
```

打开 [http://127.0.0.1:8791](http://127.0.0.1:8791)。页面默认只绑定本机回环地址，避免 Cookie 管理界面暴露到公网。

首次启动时会导入以下可选本地文件；随后 UI 保存的 `data/config.json` 是唯一配置来源：

- `secret/cookie_json`：**优先使用**的 JSON 账号数组；每项可填 `name`、`cookie`、`user-agent`，也可在网页中补充域名和专属代理。
- `secret/cookie`：兼容旧格式 `&` 分隔 Cookie 或上文 JSON 格式；仅在不存在 `cookie_json` 时导入。
- `secret/proxy`：每行一条代理，支持 `http://user:pass@host:port`、`user:pass@host:port`、`host:port:user:pass` 与 `host:port`。

`secret/` 与 `data/` 已被 `.gitignore` 排除，**不要提交 Cookie、代理账号或 `data/config.json`**。

### Web 管理

- 默认显示一个账号卡片，通过 **＋ 添加账号** / **－ 删除** 管理账号；每张卡独立填写 Cookie、可选登录域名与专属代理。User-Agent 字段仅用于诊断；Camoufox 会管理与浏览器一致的指纹，避免手工覆写造成 Cloudflare 指纹不一致。
- 未填写专属代理的账号按「公共代理池」轮换。代理失败不会回退到容器直接联网。
- 可设置每天北京时间的自动签到开关与时间，也可以立即签到全部，或对单个账号点击「单独签到」。
- 最终结果会显示签到状态、积分、剩余天数、兑换结果、出口 IP 和脱敏后的代理主机；实时日志不会显示 Cookie 或代理密码。

容器使用有头 Camoufox + Xvfb，首次构建会下载浏览器，镜像较大且需要约 1 GB 共享内存。每个账号在运行结束后删除临时浏览器 profile。

### 容器内重新登录

在 Windows 等其他环境登录得到的 Cookie，到容器里签到时可能被拒绝，接口返回 `device-mismatch`（当前浏览器和登录设备不一致）。改 User-Agent 不能把旧会话变成容器浏览器的会话。需要用容器里的 Camoufox 重新登录，让站点把新会话发给这一个浏览器。

在账号卡填写登录邮箱和登录域名，点击 **重新登录**，再点 **发送验证码**。验证码发到邮箱后，填在管理页里并确认。登录成功后，服务只更新 `data/config.json` 里该账号的 Cookie、域名和邮箱，页面上的 Cookie 框会换成新值。之后的容器签到使用这组 Cookie。

登录进行时会占用唯一的浏览器任务，定时签到会跳过这一轮。验证码最多等待 3 分钟。取消、超时或登录失败都不会改掉原来的 Cookie。本机 Edge / Chrome 签到不走这次登录。

### 本机 Edge / Chrome 模式（GLaDOS Turnstile 兜底）

如果 NAS 容器内的 Camoufox 无法取得 GLaDOS 的 Turnstile token，可在 Web 页面选择 **本地浏览器**，让 NAS 调用 Windows 电脑上的真实 Chromium 浏览器完成签到。NAS 不需要安装 Edge 或 Chrome；浏览器实际运行在 Windows 上。

> **必须保持 Windows 本机服务运行。** 该模式下，Windows 电脑开机且 `local_browser.py` 正在运行，NAS 的手动/自动签到才能调用本机 Edge / Chrome。关闭终端、重启电脑或服务退出后，本地浏览器模式会失败。

在 Windows 项目目录启动服务：

```bash
python local_browser.py
```

服务默认监听 `0.0.0.0:8792`，NAS 会访问 Windows 电脑的局域网地址。例如当前部署可用时：

```text
NAS:     http://192.168.1.120:8791
Windows: http://192.168.1.114:8792/health
```

页面操作：

1. 在“浏览器模式”选择 **本地浏览器**；
2. 默认保持“本机浏览器沿用账号代理”**不勾选**，让 Edge / Chrome 走 Windows 的系统网络（例如 Clash TUN）；
3. 点击 **保存配置**，手动、批量和定时签到都会直接调用本机浏览器；
4. 单个账号也可点击账号卡片上的 **本机浏览器签到**，跳过 NAS 容器浏览器。

本机服务会优先使用 Chrome，未安装 Chrome 时自动使用 Microsoft Edge。它会接收 NAS 发送的 Cookie 以注入临时/持久浏览器 profile，因此只应在可信局域网中开放 `8792`，并使用 NAS 防火墙限制访问来源。

签到逻辑、Web API、页面和容器入口脚本通过 Compose 挂载进容器。修改这些文件后无需重新构建镜像，只需重新创建容器：

```bash
docker compose up -d --force-recreate --no-build
```

只有修改 `requirements.txt`、Dockerfile 中的系统依赖或 Camoufox 浏览器时，才需要执行 `docker compose up -d --build`。

---

## GitHub Actions 部署（不适用于 Cloudflare 人机验证）

GitHub Actions 仍保留用于未被 Cloudflare 阻止的账号：

1. Fork 本仓库。
2. 在 Fork 的仓库进入 `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`。
3. 添加 `GLADOS_COOKIES`（填入上述 JSON 数组或 Cookie 内容）。
4. 进入 `Actions` 标签页，启用工作流。
5. 可以手动触发一次 `workflow_dispatch` 检验运行状态，每天定时自动执行。

若 Action 日志出现 Cloudflare HTML / 人机验证页，请改用上方的本地容器服务。
