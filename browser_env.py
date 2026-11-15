"""Disposable native Chromium environments for Cloudflare-protected GLaDOS accounts.

This module intentionally keeps the browser page as the owner of Cloudflare
cookies, Turnstile tokens, and the current GLaDOS client protocol. Each account
uses a temporary Chromium profile which is removed after its run.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

import httpx

from turnstile import VerificationError, has_turnstile_widget, wait_for_turnstile


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
        if scheme in {"http", "https"}:
            return text
        if scheme == "socks5" and parsed.username is None and parsed.password is None:
            return text
        if scheme in {"socks", "socks5", "socks5h"}:
            raise ValueError("当前原生 Chromium 仅支持无认证 socks5:// 代理；请使用 HTTP(S) 认证代理")
        raise ValueError("仅支持 http://、https:// 或无认证 socks5:// 代理")
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
    if scheme == "socks5" and (parsed.username is not None or parsed.password is not None):
        raise BrowserEnvironmentError("原生 Chromium 不支持认证 socks5 代理")
    result = {"server": f"{scheme}://{parsed.hostname}{f':{parsed.port}' if parsed.port else ''}"}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password is not None:
        result["password"] = unquote(parsed.password)
    return result


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


def chromium_launch_args() -> list[str]:
    """Return conservative browser flags shared by checks and account sessions."""
    return [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-features=Translate,OptimizationHints",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
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
    """One disposable native Chromium profile, one account cookie, and one proxy."""

    def __init__(self, cookie: str, proxy: str = "", user_agent: str = "", log: Log | None = None, headless: bool = True):
        self.cookie = cookie
        self.headless = bool(headless)
        self.proxy = normalize_proxy(proxy) if proxy else ""
        self.user_agent = " ".join(str(user_agent or "").split())
        self.log = log or (lambda message: logger.info(message))
        self.manager: Any = None
        self.context: Any = None
        self.page: Any = None
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
            # A proxy account may never silently begin a run on the cloud VM's
            # direct datacenter IP. Fail before Chromium starts if it is unusable.
            try:
                self.exit_ip, _ = probe_exit_ip(self.proxy)
                self.log(f"{proxy_label(self.proxy)}，出口 IP {self.exit_ip}")
            except Exception as exc:
                raise BrowserEnvironmentError(f"代理出口探测失败，拒绝直连回退：{exc}") from exc
        else:
            try:
                self.exit_ip, _ = probe_exit_ip("")
                self.log(f"直连，出口 IP {self.exit_ip}")
            except Exception as exc:  # pragma: no cover - network-dependent diagnostics
                self.log(f"直连出口 IP 探测失败：{exc}")

        profile_root = Path(os.environ.get("GLADOS_BROWSER_PROFILE_DIR", "").strip() or tempfile.gettempdir())
        profile_root.mkdir(parents=True, exist_ok=True)
        self.profile = Path(tempfile.mkdtemp(prefix="glados-chromium-", dir=profile_root))
        options: dict[str, Any] = {
            "user_data_dir": str(self.profile),
            "executable_path": self.executable,
            "headless": self.headless,
            "args": chromium_launch_args(),
            "viewport": {"width": 1366, "height": 900},
        }
        if self.user_agent:
            options["user_agent"] = self.user_agent
        browser_proxy = _browser_proxy(self.proxy)
        if browser_proxy:
            options["proxy"] = browser_proxy
        try:
            self.manager = sync_playwright().start()
            self.context = self.manager.chromium.launch_persistent_context(**options)
            self.context.set_default_timeout(20_000)
            self.context.set_default_navigation_timeout(60_000)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            mode = "无头" if self.headless else "有头 Xvfb"
            self.log(f"已创建隔离 Chromium 环境（{mode}，{proxy_label(self.proxy)}，浏览器 {Path(self.executable).name}）")
        except Exception as exc:  # pragma: no cover - depends on host Chromium
            self.close()
            raise BrowserEnvironmentError(f"启动原生 Chromium 失败：{exc}") from exc

    def close(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
        if self.manager is not None:
            try:
                self.manager.stop()
            except Exception:
                pass
        self.manager = self.context = self.page = None
        if self.profile is not None:
            shutil.rmtree(self.profile, ignore_errors=True)
            self.profile = None
        self._cookie_domains.clear()
        self._opened_domain = ""

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

    def open_domain(self, domain: str) -> None:
        self.open()
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
        if not self._page_looks_like_challenge() and not has_turnstile_widget(self.page):
            return
        self.log(f"{domain} 出现 Cloudflare 验证，等待 Chromium 通过")
        deadline = time.monotonic() + 45
        if has_turnstile_widget(self.page):
            wait_for_turnstile(self.page, timeout=40, log=self.log)
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
