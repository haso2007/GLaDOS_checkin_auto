# 在 Edge / Chrome 中用 Cookie 登录

把已有的 GLaDOS 会话 Cookie 写进本机 Microsoft Edge 或 Chrome（以及其他 Chromium 浏览器），打开已登录的控制台。用来核对某组 Cookie 是否仍然有效、查看账户页面。

这是手工登录，和下面两条自动路径分开：

- 容器里的 Camoufox 签到
- `local_browser.py` 接收 Cookie 后注入临时浏览器配置

会话 Cookie 通常带 `HttpOnly`。地址栏页面里的 `document.cookie` 写不进去。下面两种做法都是直接改浏览器的 Cookie 库，可以写入这类字段。

本文示例一律使用占位符。不要把真实 Cookie 写进仓库、截图或提交记录。

---

## 准备

1. 准备完整 Cookie 字符串，分号分隔。至少包含下面之一：
   - `koa:sess`，通常还有 `koa:sess.sig`
   - `gld:sess`，通常还有 `gld:sess.sig`
2. 确认这组 Cookie 当初是从哪个站点取出的。域名必须一致，例如 `glados.cloud`、`railgun.info`。只写主机名，不要带 `https://`，也不要带路径。
3. 用无痕窗口操作，避免和日常已登录的账号混在同一个配置里。
4. Edge 与 Chrome 的步骤相同。扩展和开发者工具的菜单位置略有差别，下文分开写。

Cookie 字符串按分号拆开。每一段只在**第一个** `=` 处分开：左边是名称，右边整段是值。值里面如果还有 `=`，全部保留。

```text
gld:sess=<会话值>; gld:sess.sig=<签名值>
```

拆开后是两条：

| 名称 | 值 |
| --- | --- |
| `gld:sess` | `<会话值>` |
| `gld:sess.sig` | `<签名值>` |

名称里的冒号和点号是 Cookie 名的一部分，不要删掉，也不要改成下划线。`koa:sess` 与 `koa:sess.sig` 用同样的方式拆。

---

## 方法一：Cookie-Editor 导入

适合 Cookie 比较长、不想逐条手打的情况。本仓库按这个流程在 Edge / Chrome 的 Cookie-Editor 里导入后，可以打开已登录的签到页。

### 1. 安装扩展

在当前浏览器的扩展商店搜索 **Cookie-Editor** 并安装。安装后把它固定到工具栏，方便在目标站点标签页上打开。

### 2. 先打开目标站点

在无痕窗口地址栏打开站点首页，例如：

```text
https://glados.cloud/
```

保持这个标签页为当前页。后面的 `domain` 要和地址栏里的主机名一致。

### 3. 打开导入框

点击工具栏里的 Cookie-Editor 图标，进入 **Import**。导入框支持三种格式：

- JSON
- Header string
- Netscape

下面给出前两种可以直接粘贴的写法。Netscape 格式这里不展开。

### 4. 粘贴 JSON

把下面整段贴进导入框。把 `domain` 改成你的站点主机名，把尖括号里的占位符换成真实的值。有几段 Cookie 就写几个对象；`koa:` 和 `gld:` 可以同时出现。

```json
[
  {
    "name": "gld:sess",
    "value": "<会话值>",
    "domain": "glados.cloud",
    "path": "/",
    "secure": true,
    "httpOnly": true,
    "sameSite": "lax",
    "session": true,
    "hostOnly": true
  },
  {
    "name": "gld:sess.sig",
    "value": "<签名值>",
    "domain": "glados.cloud",
    "path": "/",
    "secure": true,
    "httpOnly": true,
    "sameSite": "lax",
    "session": true,
    "hostOnly": true
  }
]
```

字段含义：

| 字段 | 填写 |
| --- | --- |
| `name` | 等号左边的名称，例如 `gld:sess` |
| `value` | 等号右边的完整值 |
| `domain` | 地址栏主机名，例如 `glados.cloud` |
| `path` | `/` |
| `secure` | `true`。只通过 HTTPS 发送 |
| `httpOnly` | `true`。页面脚本读不到这条 Cookie |
| `sameSite` | `lax` |
| `session` | `true`。浏览器关闭前一直有效，不另设过期时间 |
| `hostOnly` | `true`。只发给这个主机，不发给其子域名 |

两个对象的 `domain` 必须相同，并且等于你要打开的站点。

### 5. 也可以粘贴 Header string

同一个导入框接受浏览器请求头里的那种写法，不必再包一层 JSON：

```text
gld:sess=<会话值>; gld:sess.sig=<签名值>
```

用这种写法时，扩展会把 Cookie 记到**当前标签页的站点**上。所以要先打开正确的域名，再点导入。JSON 写法里的 `domain` 则由文件内容决定，当前标签页仍然建议停在同一个站点上，方便马上刷新验证。

### 6. 确认已经写入

导入后，Cookie-Editor 的列表里应能看到 `gld:sess` 或 `koa:sess`，以及对应的 `.sig`。名称、域名、路径要和上面的表一致。

---

## 方法二：开发者工具里逐条手写

不安装扩展时用这个方法。每一段 Cookie 在表格里新增一行。

### 1. 打开站点

无痕窗口先访问：

```text
https://glados.cloud/
```

先打开页面，左侧 Cookie 列表里才会出现这个源。

### 2. 打开 Cookie 表

按 `F12`。

- Edge：上方选 **应用程序**。左侧 **Cookie**（在“存储”下面），点 `https://glados.cloud`。
- Chrome：上方选 **Application**。左侧 **Cookies**，点 `https://glados.cloud`。

界面语言如果是英文，对应名称是 Application → Cookies。

### 3. 新增一行

在表格空白处双击，出现可编辑的新行。对每一段 Cookie 填这些列：

| 列 | 填写 |
| --- | --- |
| Name | `gld:sess` 或 `koa:sess` 等，保留冒号和 `.sig` |
| Value | 第一个 `=` 右边的整段值 |
| Domain | `glados.cloud`。不要写协议，不要加路径 |
| Path | `/` |
| Expires / Max-Age | 留空，表示会话 Cookie |
| Size | 自动计算，不用填 |
| HttpOnly | 勾选 |
| Secure | 勾选 |
| SameSite | `Lax` |

`gld:sess.sig`、`koa:sess`、`koa:sess.sig` 各占一行，Domain 和 Path 与第一条相同。

表格里改完一行后，点击表格外面或按回车，让这一行保存。刷新扩展或重新点一次左侧的站点，确认行还在。

### 4. 值不要被截断

从编辑器复制时，取分号之间的完整一段。常见的截断是只复制了前几个字符，或者把 `.sig` 和会话值粘进了同一格。名称格里只能有名称，值格里不能再带 `名称=`。

---

## 验证

Cookie 写完后，在同一窗口打开签到页并刷新一次：

```text
https://glados.cloud/console/checkin
```

域名改成你实际写入的主机名。页面进入已登录的控制台，就说明这组 Cookie 和这个域名匹配。

仍停在登录页时，按下面顺序检查：

1. 开发者工具的 Cookie 表里，会话字段还在，值没有缺尾。
2. Domain 是纯主机名，Path 是 `/`，Secure 已勾上。
3. `.sig` 和对应的会话 Cookie 都在。只有 `gld:sess` 而没有 `gld:sess.sig` 时，站点会把它当成未登录。
4. 换这组 Cookie 原来所属的域名再写一次。候选域名包括 `glados.cloud`、`railgun.info`、`glados.network`、`glados.rocks`、`glados.one`、`glados.space`、`glados.vip`、`glados-facility.com`。Cookie 不会跨域名通用。
5. 页面如果停在 Cloudflare 验证，先在这个真实浏览器里把验证做完，再刷新签到页。Cookie 本身不会代替人机验证。
6. 以上都对、仍然是登录页，说明会话已经过期。重新在网页登录，取出新的 Cookie 再导入。

---

## 换账号或重做

同一个站点上再导入另一组 Cookie 时，先在 Cookie-Editor 或开发者工具里删掉旧的 `koa:sess`、`koa:sess.sig`、`gld:sess`、`gld:sess.sig`，再写入新的。旧会话留着时，浏览器可能继续带上失效的那一组。

要同时保留多个账号，为每个账号单独使用一个浏览器配置（Chrome / Edge 的“人员”或配置文件），或者每次都用新的无痕窗口并只保留当前这一组。
