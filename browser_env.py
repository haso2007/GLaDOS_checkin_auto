"""Isolated Camoufox environments for Cloudflare-protected GLaDOS accounts."""

from __future__ import annotations

import logging
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
    """A browser environment or browser-context request failed."""


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
    """Accept the proxy formats supported by the existing Proxy001 console."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        parsed = urlparse(text)
        if not parsed.hostname:
            raise ValueError("无法解析代理地址")
        return text
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
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.hostname:
        raise BrowserEnvironmentError("代理地址缺少主机名")
    scheme = (parsed.scheme or "http").lower()
    if scheme in {"socks", "socks5", "socks5h"} and parsed.username:
        # Firefox cannot authenticate to SOCKS5; preserve the existing console behavior.
        scheme = "http"
    elif scheme == "socks5h":
        scheme = "socks5"
    server = f"{scheme}://{parsed.hostname}{f':{parsed.port}' if parsed.port else ''}"
    result = {"server": server}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password is not None:
        result["password"] = unquote(parsed.password)
    return result


def probe_exit_ip(proxy: str) -> tuple[str, str]:
    """Get proxy exit information without inheriting host proxy environment variables."""
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


class BrowserEnvironment:
    """One disposable browser profile, one account cookie, and one proxy."""

    def __init__(self, cookie: str, proxy: str = "", user_agent: str = "", log: Log | None = None, headless: bool = False):
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
            from camoufox.sync_api import Camoufox
        except ImportError as exc:
            raise BrowserEnvironmentError("未安装 Camoufox；请重新构建容器或安装 requirements.txt") from exc

        geoip: str | bool = False
        try:
            self.exit_ip, _ = probe_exit_ip(self.proxy)
            geoip = self.exit_ip
            self.log(f"{proxy_label(self.proxy)}，出口 IP {self.exit_ip}")
        except Exception as exc:  # pragma: no cover - network-dependent fallback
            if self.proxy:
                geoip = True
            self.log(f"出口 IP 探测失败，将继续尝试浏览器自动地理定位：{exc}")

        # Camoufox also performs its own GeoIP lookup when ``geoip`` is set.
        # Dynamic proxies occasionally time out on that second lookup, even
        # after our trace request succeeded.  Retry with progressively less
        # GeoIP automation instead of rejecting a usable proxy outright.
        variants: list[dict[str, Any]] = []
        if self.exit_ip:
            variants.append({"geoip": self.exit_ip, "block_webrtc": True})
        elif self.proxy:
            variants.append({"geoip": True, "block_webrtc": True})
        variants.extend(
            [
                {"geoip": False, "block_webrtc": True},
                {"geoip": False, "block_webrtc": False},
            ]
        )
        browser_proxy = _browser_proxy(self.proxy)
        last_error: Exception | None = None

        for variant in variants:
            profile = Path(tempfile.mkdtemp(prefix="glados-cf-"))
            prefs = {
                "dom.ipc.processCount": 1,
                "dom.ipc.processCount.webIsolated": 1,
                "accessibility.force_disabled": 1,
                "toolkit.telemetry.enabled": False,
                "app.normandy.enabled": False,
                "browser.discovery.enabled": False,
                "media.autoplay.default": 5,
            }
            if self.proxy:
                # Never fall back to the container IP when an account proxy becomes unavailable.
                prefs["network.proxy.failover_direct"] = False
                prefs["network.proxy.socks_remote_dns"] = True

            options: dict[str, Any] = {
                "headless": self.headless,
                # Humanized pointer movement is what lets grok-register pass Turnstile.
                "humanize": True,
                "i_know_what_im_doing": True,
                "persistent_context": True,
                "user_data_dir": str(profile),
                "os": ("windows",),
                "geoip": variant["geoip"],
                "block_webrtc": variant["block_webrtc"],
                "firefox_user_prefs": prefs,
            }
            try:
                from camoufox.addons import DefaultAddons

                # uBlock can prevent the explicit check-in Turnstile widget from issuing a token.
                options["exclude_addons"] = [DefaultAddons.UBO]
            except Exception:
                pass
            # Camoufox owns the coordinated User-Agent / navigator fingerprint.
            # Do not pass a raw per-account UA: it is not a Camoufox launch
            # option and an inconsistent override makes Cloudflare detection
            # more likely.
            if browser_proxy:
                options["proxy"] = browser_proxy

            manager: Any = None
            try:
                manager = Camoufox(**options)
                context = manager.__enter__()
                context.set_default_timeout(20_000)
                context.set_default_navigation_timeout(60_000)
                self.manager = manager
                self.context = context
                self.page = context.new_page()
                self.profile = profile
                self.log(
                    f"已创建隔离浏览器环境（{'无头' if self.headless else '有头'}，{proxy_label(self.proxy)}，"
                    f"geoip={variant['geoip']}，webrtc_block={variant['block_webrtc']}）"
                )
                return
            except Exception as exc:  # pragma: no cover - depends on host browser/proxy
                last_error = exc
                if manager is not None:
                    try:
                        manager.__exit__(None, None, None)
                    except Exception:
                        pass
                shutil.rmtree(profile, ignore_errors=True)
                self.log(f"浏览器启动参数不兼容，改用下一组：{exc}")

        raise BrowserEnvironmentError(f"启动 Camoufox 失败：{last_error}")

    def close(self) -> None:
        if self.manager is not None:
            try:
                self.manager.__exit__(None, None, None)
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
        """Clear the visible Cloudflare page, if it has already appeared."""
        if not self._page_looks_like_challenge() and not has_turnstile_widget(self.page):
            return
        self.log(f"{domain} 出现 Cloudflare 验证，等待浏览器自动通过")
        deadline = time.monotonic() + 45
        if has_turnstile_widget(self.page):
            wait_for_turnstile(self.page, timeout=40, log=self.log)
        while time.monotonic() < deadline:
            if not self._page_looks_like_challenge():
                self.log("Cloudflare 页面验证已通过")
                return
            try:
                self.page.wait_for_timeout(750)
            except Exception:
                time.sleep(0.75)
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
        """Make a same-origin request so CF cookies and browser TLS state stay together."""
        self.open_domain(domain)
        response = self._fetch(domain, method, path, data, json_body)
        if response.is_cloudflare_challenge:
            self.log(f"{path} 被 Cloudflare 拦截，尝试完成人机验证")
            self._clear_page_challenge(domain)
            response = self._fetch(domain, method, path, data, json_body)
        return response
