# GLaDOS 原生 Chromium 自动签到

此分支将 GLaDOS 每日签到、状态/积分查询、临期积分兑换、失败重试和定时任务运行在一台 Debian 云服务器上。每个账号都使用独立、一次性的 Chromium profile；页面签到、Cloudflare Turnstile、Cookie、状态和兑换请求保持在同一真实浏览器上下文内。

> 当前分支：`feat/instabox-debian-native-chromium`
>
> 目标环境：Instabox Debian 13+、系统 Chromium、systemd。
> 此分支**不使用 Docker**，也不包含 Windows Edge/Chrome 回退服务。

## 安全与 Cloudflare 限制

- 云服务器的 Chromium 不是绕过 Cloudflare 的方法。数据中心出口 IP、代理信誉、地理位置、Linux/无头浏览器特征或 Cookie 登录设备差异都可能使 Turnstile 无法通过。
- 服务只点击 GLaDOS 自己的签到页面按钮并等待网页产生/提交验证结果；不会伪造、重放、导出或记录 Turnstile token，也不会调用打码服务。
- 账号设置了专属代理、或使用公共代理池时，代理出口探测失败会使该代理尝试失败，**绝不静默改用云服务器直连 IP**。
- 管理页面能读取和写入 Cookie、代理地址与代理密码。仅可通过你自行保护的 Cloudflare Tunnel/Access 发布，切勿直接公开端口。
- Cookie、代理凭据、Turnstile token 和其他机密不得提交到 Git、写入日志或复制到应用发布目录。

## Instabox Debian 安装

在 Instabox Web Terminal 中克隆已发布的分支：

```bash
git clone --branch feat/instabox-debian-native-chromium \
  https://github.com/haso2007/GLaDOS_checkin_auto.git
cd GLaDOS_checkin_auto
sudo bash deploy/debian/setup.sh
```

安装脚本会：

1. 安装 Debian 的 `chromium`、Python venv、Xvfb、字体与必需工具；不会运行 `playwright install` 或下载 Playwright 自带浏览器。
2. 创建无登录权限的 `glados` 系统用户。
3. 将 root 所有的应用发布到 `/opt/glados-checkin`，将敏感且持久的数据保存在 `/var/lib/glados-checkin`。
4. 安装 `glados-checkin.service`，默认只监听 `127.0.0.1:3000`。
5. 在真实 systemd 服务环境中检查 HTTP readiness，并启动/关闭一个不访问网络、不带账号 Cookie 的 Chromium `about:blank` 自检。

安装过程中会先构建并验证临时发布目录，再停止旧服务切换版本；失败时会恢复上一版。账号配置和日志目录不会因升级而删除。

> 首次安装前，此分支必须已经提交并推送到远端；否则 `git clone --branch` 只能取得旧版本。

## 服务管理与诊断

```bash
# 查看当前状态
sudo systemctl status glados-checkin

# 跟踪服务日志
sudo journalctl -u glados-checkin -f

# 标准 readiness：检查 API 与 Chromium 可执行文件
curl -fsS http://127.0.0.1:3000/api/health

# 深度本地 Chromium 自检：仅打开 about:blank，不会访问 GLaDOS
curl -fsS 'http://127.0.0.1:3000/api/health?deep=true'

# 重启服务
sudo systemctl restart glados-checkin
```

服务环境文件位于 `/etc/glados-checkin/glados-checkin.env`：

```bash
WEB_HOST=127.0.0.1
WEB_PORT=3000
GLADOS_DATA_DIR=/var/lib/glados-checkin
GLADOS_BROWSER_PROFILE_DIR=/var/lib/glados-checkin/profiles
GLADOS_CHROMIUM_EXECUTABLE=
```

`WEB_HOST` 在此分支必须保持回环地址。若你的 Tunnel 无法转发 `127.0.0.1:3000`，请先明确网络和防火墙策略，再调整绑定地址；本仓库不会配置 Cloudflare Tunnel、域名、TLS 或外部认证。

浏览器模式由 Web UI 保存的配置控制：

- **无头**：默认的云端运行模式；
- **有头（Xvfb）**：仅用于受控诊断。窗口只出现在服务的私有虚拟显示器中，不会开启 VNC、RDP、浏览器调试端口或显示 TCP 端口。

服务保留 `NoNewPrivileges=true`，并依赖 Chromium 自己的沙箱；绝不应通过添加 `--no-sandbox` 来解决启动问题。若实际 Instabox systemd 日志证明该限制阻碍 Chromium 的受支持沙箱初始化，应先保留日志并单独评估最小化调整，而不是降低浏览器或文件权限安全性。

## Web 管理与数据持久化

打开你的受保护 Tunnel 地址后，在页面中：

1. 通过“批量导入 Cookie”或账号卡片添加 Cookie；首次配置推荐使用 JSON 数组。
2. 可为账号填写可选域名、专属代理，或在公共代理池逐行添加代理。
3. 可选择 Chromium **无头**或**有头（Xvfb）**模式，设置自动兑换和北京时间每日定时签到。
4. 可手动执行全部/单账号签到，并在结果持久化后重试失败账号。

敏感配置和运行状态：

```text
/var/lib/glados-checkin/config.json
/var/lib/glados-checkin/job_state.json
/var/lib/glados-checkin/scheduler_state.json
/var/lib/glados-checkin/logs/glados.log
```

目录归 `glados` 用户私有（`0700`），服务文件 `UMask=0077`。日志会脱敏常见 Cookie 和代理密码字段，历史日志上限为 10 MB。最新运行结果和每日调度 key 单独持久化：服务在同一分钟重启不会重复启动已接受的定时任务；若服务在签到过程中重启，界面会标记该任务中断，已保存的失败结果仍可用于重试。

Cookie 与代理配置默认只经由 Web UI 写入私有数据目录。安装脚本有意不复制克隆目录中的 `secret/`、`data/`、`.env` 或 Git 元数据；不要把真实凭据放在应用目录或仓库中。

### Cookie JSON 格式

```json
[
  {
    "name": "主号",
    "origin": "glados.cloud",
    "cookie": "koa:sess=...; koa:sess.sig=...; gld:sess=...",
    "user-agent": "Mozilla/5.0 (...)"
  }
]
```

- `cookie` 必须包含 `koa:sess=` 或 `gld:sess=`。
- 在本机 Edge 或 Chrome 里手工写入已有 Cookie 的步骤见 [BROWSER_COOKIE_LOGIN.md](BROWSER_COOKIE_LOGIN.md)。

### 设备不匹配时重新登录

在其他系统登录得到的 Cookie，到这台 Debian 服务器的 Chromium 上签到时，可能被拒绝为 `device-mismatch`。改 User-Agent 不能把旧会话变成当前浏览器的会话。

在账号卡填写登录邮箱和登录域名，点击 **重新登录**，再点 **发送验证码**。验证码发到邮箱后，填在管理页里并确认。登录由本服务的原生 Chromium 完成，成功后只更新 `/var/lib/glados-checkin/config.json` 里该账号的 Cookie、域名和邮箱。之后的签到使用这组 Cookie。

登录进行时会占用唯一的浏览器任务，定时签到会跳过这一轮。验证码最多等待 3 分钟。取消、超时或登录失败都不会改掉原来的 Cookie。使用的无头或有头模式与当前签到配置相同。
- `name`、`origin`、`user-agent` 可选；缺少域名时会探测候选 GLaDOS 域名。
- `user-agent` 应是获取该 Cookie 时的浏览器 UA。原生 Chromium 会使用该请求 UA，但不会模拟完整的操作系统或浏览器指纹。
- 支持旧版 `cookie1&cookie2` 格式。
- 支持 `http://`、`https://` 代理及无认证的 `socks5://` 代理。认证 SOCKS 代理会被明确拒绝而不是协议篡改或直连回退。

## 自动兑换

当剩余天数小于等于设定阈值（默认 `1` 天）时，服务会按积分兑换可用的最大档位：

| 积分 | 兑换天数 | `planType` |
| ---: | ---: | --- |
| 500+ | 100 | `plan500` |
| 200+ | 30 | `plan200` |
| 100+ | 10 | `plan100` |

## 更新与备份

```bash
cd GLaDOS_checkin_auto
git pull --ff-only
sudo bash deploy/debian/setup.sh
```

升级前可在受限权限位置备份持久数据：

```bash
sudo tar -C /var/lib -czf glados-checkin-backup.tar.gz glados-checkin
```

不要将该备份上传到公开位置。安装成功后，脚本会提示是否有旧应用发布目录暂留；确认新版本稳定后可按提示删除旧目录。

## GitHub Actions（仅 HTTP 最佳尝试）

GitHub Actions 继续保留纯 HTTP 签到路径，依赖文件为 `requirements-cli.txt`。它不运行浏览器，也不适用于需要 Cloudflare/Turnstile 验证的账号。Cloudflare 受阻时，请使用本分支的原生 Chromium 服务并检查浏览器/代理诊断，而不是尝试伪造验证 token。
