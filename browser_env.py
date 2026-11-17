"""Disposable native Chromium environments for Cloudflare-protected GLaDOS accounts.

This module intentionally keeps the browser page as the owner of Cloudflare
cookies, Turnstile tokens, and the current GLaDOS client protocol. Each account
opens its own Chromium profile and does not share that profile with another account.
"""

from __future__ import annotations

import base64
import logging
import os
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

import httpx

from browser_state import saved_cookies, save_browser_state
from turnstile import VerificationError, wait_for_turnstile


logger = logging.getLogger(__name__)
Log = Callable[[str], None]


class BrowserEnvironmentError(RuntimeError):
    """A native browser environment or browser-context request failed."""


@dataclass(frozen=True)
class BrowserResponse:
    status: int
    body: Any
    text: str
    content_type: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def is_cloudflare_challenge(self) -> bool:
        text = self.text.lower()
        return (
            self.status in (403, 429, 503)
            and ("cloudflare" in text or "just a moment" in text or "checking your browser" in text)
        ) or "cf-turnstile" in text or "challenges.cloudflare.com" in text


def normalize_proxy(value: str) -> str:
    """Accept only proxy forms that Chromium can use without protocol mutation."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        parsed = urlparse(text)
        scheme = parsed.scheme.lower()
        if not parsed.hostname:
            raise ValueError("无法解析代理地址")
        if scheme in {"http", "https", "socks5"}:
            return text
        if scheme in {"socks", "socks5h"}:
            return text.replace(f"{parsed.scheme}://", "socks5://", 1)
        raise ValueError("仅支持 http://、https:// 或 socks5:// 代理")
    if "@" in text:
        return f"http://{text}"
    parts = text.split(":")
    if len(parts) == 4 and parts[1].isdigit():
        host, port, user, password = parts
        return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    if len(parts) == 2 and parts[1].isdigit():
        return f"http://{text}"
    raise ValueError("代理格式应为 scheme://user:pass@host:port、user:pass@host:port 或 host:port")


def parse_proxy_lines(text: str) -> list[str]:
    proxies: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        proxy = normalize_proxy(line)
        if proxy not in seen:
            seen.add(proxy)
            proxies.append(proxy)
    return proxies


def proxy_label(proxy: str) -> str:
    if not proxy:
        return "直连"
    parsed = urlparse(proxy)
    host = parsed.hostname or "?"
    return f"{parsed.scheme or 'http'}://{host}{f':{parsed.port}' if parsed.port else ''}"


def _browser_proxy(proxy: str) -> dict[str, str] | None:
    """Convert a previously validated proxy to Playwright's explicit shape."""
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.hostname:
        raise BrowserEnvironmentError("代理地址缺少主机名")
    scheme = (parsed.scheme or "http").lower()
    if scheme not in {"http", "https", "socks5"}:
        raise BrowserEnvironmentError("原生 Chromium 不支持该代理协议")
    result = {"server": f"{scheme}://{parsed.hostname}{f':{parsed.port}' if parsed.port else ''}"}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password is not None:
        result["password"] = unquote(parsed.password)
    return result


class _AuthProxyBridge:
    """Local HTTP proxy so Chromium can use an authenticated HTTP or SOCKS5 proxy."""

    def __init__(self, upstream: str) -> None:
        parsed = urlparse(upstream)
        if not parsed.hostname or not parsed.port:
            raise BrowserEnvironmentError("代理地址缺少主机或端口")
        self._scheme = (parsed.scheme or "http").lower()
        self._host = parsed.hostname
        self._port = parsed.port
        self._user = unquote(parsed.username or "")
        self._password = unquote(parsed.password or "")
        token = base64.b64encode(f"{self._user}:{self._password}".encode()).decode()
        self._auth = f"Basic {token}"
        self._listen = socket.socket()
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind(("127.0.0.1", 0))
        self.port = int(self._listen.getsockname()[1])
        self._listen.listen(32)
        self._closed = False
        threading.Thread(target=self._accept, name="glados-proxy-bridge", daemon=True).start()

    def close(self) -> None:
        self._closed = True
        try:
            self._listen.close()
        except OSError:
            pass

    def _accept(self) -> None:
        while not self._closed:
            try:
                client, _ = self._listen.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data += chunk
            head, rest = data.split(b"\r\n\r\n", 1)
            parts = head.decode("latin1").split("\r\n")[0].split(" ")
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            target = parts[1]
            host, _, port_text = target.rpartition(":")
            upstream = self._open_upstream(host, int(port_text or "443"))
            if self._scheme == "socks5":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
                self._splice(client, upstream)
                return
            request = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: {self._auth}\r\n\r\n"
            upstream.sendall(request.encode())
            response = b""
            while b"\r\n\r\n" not in response:
                block = upstream.recv(4096)
                if not block:
                    break
                response += block
            status = response.split(b" ", 2)[1] if response.startswith(b"HTTP/") else b"502"
            if status != b"200":
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            self._splice(client, upstream)
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except OSError:
                pass
        finally:
            client.close()
            if upstream is not None:
                upstream.close()

    def _splice(self, client: socket.socket, upstream: socket.socket) -> None:
        sockets = [client, upstream]
        while True:
            readable, _, _ = select.select(sockets, [], [], 60)
            if not readable:
                return
            for source in readable:
                payload = source.recv(65536)
                if not payload:
                    return
                (upstream if source is client else client).sendall(payload)

    def _open_upstream(self, host: str, port: int) -> socket.socket:
        if self._scheme != "socks5":
            return socket.create_connection((self._host, self._port), 20)
        sock = socket.create_connection((self._host, self._port), 20)
        try:
            self._socks5_connect(sock, host, port)
        except Exception:
            sock.close()
            raise
        return sock

    def _socks5_connect(self, sock: socket.socket, host: str, port: int) -> None:
        if self._user:
            sock.sendall(b"\x05\x01\x02")
            chosen = self._recvn(sock, 2)
            if chosen[1] != 2:
                raise BrowserEnvironmentError("SOCKS5 代理未接受用户名认证")
            user = self._user.encode()
            password = self._password.encode()
            sock.sendall(bytes([1, len(user)]) + user + bytes([len(password)]) + password)
            verdict = self._recvn(sock, 2)
            if verdict[1] != 0:
                raise BrowserEnvironmentError("SOCKS5 代理认证失败")
        else:
            sock.sendall(b"\x05\x01\x00")
            chosen = self._recvn(sock, 2)
            if chosen[1] != 0:
                raise BrowserEnvironmentError("SOCKS5 代理未接受无认证连接")
        host_bytes = host.encode()
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + port.to_bytes(2, "big"))
        header = self._recvn(sock, 4)
        if header[1] != 0:
            raise BrowserEnvironmentError(f"SOCKS5 连接失败，代码 {header[1]}")
        if header[3] == 1:
            self._recvn(sock, 6)
        elif header[3] == 4:
            self._recvn(sock, 18)
        elif header[3] == 3:
            length = self._recvn(sock, 1)[0]
            self._recvn(sock, length + 2)

    @staticmethod
    def _recvn(sock: socket.socket, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = sock.recv(count - len(data))
            if not chunk:
                raise BrowserEnvironmentError("SOCKS5 代理提前断开")
            data += chunk
        return data


def probe_exit_ip(proxy: str) -> tuple[str, str]:
    """Get proxy exit information without inheriting unrelated host proxy variables."""
    kwargs: dict[str, Any] = {"timeout": 15, "follow_redirects": True, "trust_env": False}
    if proxy:
        kwargs["proxy"] = proxy
    with httpx.Client(**kwargs) as client:
        response = client.get("https://www.cloudflare.com/cdn-cgi/trace")
        response.raise_for_status()
    trace = response.text
    for line in trace.splitlines():
        if line.startswith("ip="):
            return line[3:].strip(), trace
    raise BrowserEnvironmentError("Cloudflare trace 响应中没有出口 IP")


def _cookie_pairs(raw_cookie: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in str(raw_cookie or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key:
            pairs.append((key.strip(), value.strip()))
    return pairs


def browser_display() -> str:
    """Use the desktop screen when it exists. The service itself stays on Xvfb."""
    configured = os.environ.get("GLADOS_BROWSER_DISPLAY", "").strip()
    if configured:
        return configured
    if Path("/tmp/.X11-unix/X1").exists():
        return ":1"
    return os.environ.get("DISPLAY", "").strip()


def chromium_executable() -> str:
    """Resolve the distro-managed Chromium binary without downloading one."""
    configured = os.environ.get("GLADOS_CHROMIUM_EXECUTABLE", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(filter(None, (shutil.which("chromium"), shutil.which("chromium-browser"), "/usr/bin/chromium")))
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise BrowserEnvironmentError(
        "未找到原生 Chromium；请安装 Debian chromium 或设置 GLADOS_CHROMIUM_EXECUTABLE"
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_cdp(port: int) -> None:
    deadline = time.monotonic() + 20
    url = f"http://127.0.0.1:{port}/json/version"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.4) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise BrowserEnvironmentError("Chromium 调试端口没有就绪")


def chromium_launch_args() -> list[str]:
    """Return conservative browser flags shared by checks and account sessions."""
    return [
        "--no-first-run",
        "--no-default-browser-check",
    ]


def smoke_chromium() -> str:
    """Launch and close a no-network Chromium page without account data."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserEnvironmentError("未安装 Playwright；请安装原生 Debian 服务依赖") from exc

    executable = chromium_executable()
    profile_root = Path(os.environ.get("GLADOS_BROWSER_PROFILE_DIR", "").strip() or tempfile.gettempdir())
    profile_root.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="glados-chromium-smoke-", dir=profile_root))
    manager: Any = None
    context: Any = None
    try:
        manager = sync_playwright().start()
        context = manager.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            executable_path=executable,
            headless=True,
            args=chromium_launch_args(),
            viewport={"width": 1366, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("about:blank", wait_until="commit", timeout=15_000)
        return executable
    except Exception as exc:  # pragma: no cover - depends on host Chromium
        raise BrowserEnvironmentError(f"原生 Chromium 自检失败：{exc}") from exc
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if manager is not None:
            try:
                manager.stop()
            except Exception:
                pass
        shutil.rmtree(profile, ignore_errors=True)


class BrowserEnvironment:
    """One native Chromium profile for one account. The profile stays on disk."""

    _active: "BrowserEnvironment | None" = None
    _active_lock = threading.Lock()

    def __init__(
        self,
        cookie: str,
        proxy: str = "",
        user_agent: str = "",
        log: Log | None = None,
        headless: bool = True,
        state_dir: Path | None = None,
    ):
        self.cookie = cookie
        self.headless = bool(headless)
        self.state_dir = Path(state_dir) if state_dir else None
        self._keep_profile = self.state_dir is not None
        self.proxy = normalize_proxy(proxy) if proxy else ""
        self.user_agent = " ".join(str(user_agent or "").split())
        self.log = log or (lambda message: logger.info(message))
        self.manager: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self._process: subprocess.Popen[bytes] | None = None
        self._proxy_bridge: _AuthProxyBridge | None = None
        self.profile: Path | None = None
        self.exit_ip = ""
        self.executable = ""
        self._cookie_domains: set[str] = set()
        self._opened_domain = ""

    def __enter__(self) -> "BrowserEnvironment":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False

    def open(self) -> None:
        if self.context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserEnvironmentError("未安装 Playwright；请安装原生 Debian 服务依赖") from exc

        self.executable = chromium_executable()
        if self.proxy:
            # Keep the configured proxy even when the exit probe fails. Do not
            # fall back to the machine's direct address.
            try:
                self.exit_ip, _ = probe_exit_ip(self.proxy)
                self.log(f"{proxy_label(self.proxy)}，出口 IP {self.exit_ip}")
            except Exception as exc:
                self.log(f"{proxy_label(self.proxy)}，出口探测失败，仍通过该代理打开浏览器：{exc}")
        else:
            self.log("直连，不在打开浏览器前访问 Cloudflare")

        if self.state_dir is not None:
            self.profile = self.state_dir / "user-data-dir"
            self.profile.mkdir(parents=True, exist_ok=True)
            for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                try:
                    (self.profile / lock_name).unlink()
                except OSError:
                    pass
            self.log(f"打开该账号的独立浏览器环境 {self.state_dir.name}")
        else:
            profile_root = Path(os.environ.get("GLADOS_BROWSER_PROFILE_DIR", "").strip() or tempfile.gettempdir())
            profile_root.mkdir(parents=True, exist_ok=True)
            self.profile = Path(tempfile.mkdtemp(prefix="glados-chromium-", dir=profile_root))
        port = _free_port()
        command = [
            self.executable,
            f"--user-data-dir={self.profile}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--window-size=1366,900",
            *chromium_launch_args(),
        ]
        if self.headless:
            command.append("--headless=new")
        if self.user_agent:
            self.log("已忽略账号里的 User-Agent，避免和当前 Linux Chromium 不一致")
        browser_proxy = _browser_proxy(self.proxy)
        if browser_proxy:
            server = browser_proxy["server"]
            if browser_proxy.get("username"):
                self._proxy_bridge = _AuthProxyBridge(self.proxy)
                server = f"http://127.0.0.1:{self._proxy_bridge.port}"
                kind = "SOCKS5" if browser_proxy["server"].startswith("socks5://") else "HTTP"
                self.log(f"已为带认证的 {kind} 代理建立本地转发")
            command.append(f"--proxy-server={server}")
        command.append("about:blank")
        launch_env = os.environ.copy()
        display = browser_display()
        if display and not self.headless:
            launch_env["DISPLAY"] = display
            self.log(f"浏览器打开在桌面屏幕 {display}")
        try:
            self._process = subprocess.Popen(
                command,
                env=launch_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            with BrowserEnvironment._active_lock:
                BrowserEnvironment._active = self
            _wait_for_cdp(port)
            self.manager = sync_playwright().start()
            self.browser = self.manager.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
            self.context.set_default_timeout(20_000)
            self.context.set_default_navigation_timeout(60_000)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            mode = "无头" if self.headless else f"桌面 {display or '当前屏幕'}"
            self.log(f"系统 Chromium 已启动（{mode}，{proxy_label(self.proxy)}，浏览器 {Path(self.executable).name}）")
        except Exception as exc:  # pragma: no cover - depends on host Chromium
            self.close()
            raise BrowserEnvironmentError(f"启动原生 Chromium 失败：{exc}") from exc

    @classmethod
    def close_active(cls) -> None:
        """Stop the browser that is currently signing in or logging in."""
        with cls._active_lock:
            current = cls._active
        if current is not None:
            current.close()

    def close(self) -> None:
        with BrowserEnvironment._active_lock:
            if BrowserEnvironment._active is self:
                BrowserEnvironment._active = None
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
        if self.manager is not None:
            try:
                self.manager.stop()
            except Exception:
                pass
        bridge = self._proxy_bridge
        self._proxy_bridge = None
        if bridge is not None:
            bridge.close()
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
        self.manager = self.browser = self.context = self.page = None
        if self.profile is not None and not self._keep_profile:
            shutil.rmtree(self.profile, ignore_errors=True)
        self.profile = None
        self._cookie_domains.clear()
        self._opened_domain = ""

    def save_state(self) -> None:
        if self.state_dir is None:
            return
        save_browser_state(self, self.state_dir)
        self.log("已保存该账号的独立浏览器缓存")

    def _install_saved_state(self, domain: str) -> None:
        """Restore only this account's saved cookies."""
        if self.state_dir is None or self.context is None:
            return
        cookies = saved_cookies(self.state_dir, domain)
        if not cookies:
            self.log("该账号还没有可恢复的浏览器缓存")
            return
        self.context.add_cookies(cookies)
        self.log(f"已恢复该账号缓存中的 {len(cookies)} 项 Cookie")

    def _install_cookies(self, domain: str) -> None:
        if domain in self._cookie_domains:
            return
        if self.context is None:
            raise BrowserEnvironmentError("浏览器未启动")
        cookies = [
            {"name": name, "value": value, "domain": domain, "path": "/", "secure": True}
            for name, value in _cookie_pairs(self.cookie)
        ]
        if not cookies:
            raise BrowserEnvironmentError("Cookie 为空或格式无法解析")
        self.context.add_cookies(cookies)
        self._cookie_domains.add(domain)

    def open_url(self, url: str) -> None:
        """Open a page without installing the account cookie."""
        self.open()
        if self.page is None:
            raise BrowserEnvironmentError("浏览器页面未启动")
        host = urlparse(url).hostname or url
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            self.page.wait_for_timeout(1_000)
        except Exception as exc:
            raise BrowserEnvironmentError(f"打开 {url} 失败：{exc}") from exc
        self._clear_page_challenge(host)

    def fetch_current(self, path: str) -> BrowserResponse:
        """Request the current page origin. Cookies already in the browser stay there."""
        if self.page is None:
            raise BrowserEnvironmentError("浏览器页面未启动")
        host = urlparse(self.page.url).hostname
        if not host:
            raise BrowserEnvironmentError("当前页面没有可用域名")
        response = self._fetch(host, "GET", path, None, False)
        if response.is_cloudflare_challenge:
            self.log(f"{path} 被 Cloudflare 拦截，尝试完成人机验证")
            self._clear_page_challenge(host)
            response = self._fetch(host, "GET", path, None, False)
        return response

    def open_domain(self, domain: str) -> None:
        self.open()
        self._install_saved_state(domain)
        self._install_cookies(domain)
        if self._opened_domain == domain:
            return
        try:
            self.page.goto(f"https://{domain}/console/checkin", wait_until="domcontentloaded", timeout=60_000)
            self.page.wait_for_timeout(1_000)
        except Exception as exc:
            raise BrowserEnvironmentError(f"打开 {domain} 失败：{exc}") from exc
        self._opened_domain = domain
        self._clear_page_challenge(domain)

    def _page_looks_like_challenge(self) -> bool:
        try:
            text = str(self.page.locator("body").inner_text(timeout=2_000) or "").lower()
            title = str(self.page.title() or "").lower()
        except Exception:
            return False
        clues = ("just a moment", "checking your browser", "verify you are human", "attention required")
        return any(clue in text or clue in title for clue in clues)

    def _clear_page_challenge(self, domain: str) -> None:
        if not self._page_looks_like_challenge():
            return
        self.log(f"{domain} 出现整页 Cloudflare 验证，等待 Chromium 通过")
        deadline = time.monotonic() + 45
        try:
            wait_for_turnstile(self.page, timeout=40, log=self.log)
        except VerificationError:
            pass
        while time.monotonic() < deadline:
            if not self._page_looks_like_challenge():
                self.log("Cloudflare 页面验证已通过")
                return
            self.page.wait_for_timeout(750)
        raise VerificationError("Cloudflare 页面验证超时")

    def _fetch(self, domain: str, method: str, path: str, data: dict[str, Any] | None, json_body: bool) -> BrowserResponse:
        if self.page is None:
            raise BrowserEnvironmentError("浏览器页面未启动")
        raw = self.page.evaluate(
            """async ({url, method, data, jsonBody}) => {
              const headers = {Accept: 'application/json, text/plain, */*'};
              let body;
              if (method !== 'GET' && method !== 'HEAD') {
                if (jsonBody) {
                  headers['Content-Type'] = 'application/json;charset=UTF-8';
                  body = JSON.stringify(data || {});
                } else {
                  headers['Content-Type'] = 'application/x-www-form-urlencoded;charset=UTF-8';
                  body = new URLSearchParams(Object.entries(data || {}).map(([k, v]) => [k, v == null ? '' : String(v)]));
                }
              }
              const ctrl = new AbortController();
              const timer = setTimeout(() => ctrl.abort(), 30000);
              try {
                const response = await fetch(url, {method, headers, body, credentials: 'same-origin', signal: ctrl.signal});
                const text = await response.text();
                let bodyValue = null;
                try { bodyValue = JSON.parse(text); } catch (_) {}
                return {status: response.status, text, contentType: response.headers.get('content-type') || '', body: bodyValue};
              } finally { clearTimeout(timer); }
            }""",
            {"url": f"https://{domain}{path}", "method": method.upper(), "data": data or {}, "jsonBody": json_body},
        )
        if not isinstance(raw, dict):
            raise BrowserEnvironmentError(f"{path} 浏览器响应格式异常")
        return BrowserResponse(
            status=int(raw.get("status") or 0),
            body=raw.get("body"),
            text=str(raw.get("text") or ""),
            content_type=str(raw.get("contentType") or ""),
        )

    def request(
        self,
        domain: str,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        json_body: bool = False,
    ) -> BrowserResponse:
        """Make a same-origin request so browser cookies and TLS state stay together."""
        self.open_domain(domain)
        response = self._fetch(domain, method, path, data, json_body)
        if response.is_cloudflare_challenge:
            self.log(f"{path} 被 Cloudflare 拦截，尝试完成人机验证")
            self._clear_page_challenge(domain)
            response = self._fetch(domain, method, path, data, json_body)
        return response
