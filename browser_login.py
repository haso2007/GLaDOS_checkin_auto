"""Log in through the container Camoufox session that will later check in.

The GLaDOS page issues the session to the browser that submits the email code.
This module drives that page and returns only the session cookies. It does not
forge a Turnstile token and it does not keep the verification code.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from browser_env import BrowserEnvironment, BrowserEnvironmentError, proxy_label
from glados import CookieEntry
from turnstile import VerificationError


Log = Callable[[str], None]

SESSION_NAMES = ("koa:sess", "koa:sess.sig", "gld:sess", "gld:sess.sig")
CODE_WAIT_SECONDS = 180


class LoginError(RuntimeError):
    """The login page could not be completed. The saved cookie must stay unchanged."""


class CodeRejected(LoginError):
    """The site refused this code. The login browser can accept another one."""


class LoginCancelled(LoginError):
    """The operator cancelled the login."""


def _host_matches(cookie_domain: str, host: str) -> bool:
    domain = str(cookie_domain or "").lstrip(".").lower()
    current = host.lower()
    return domain == current or current.endswith(f".{domain}") or domain.endswith(f".{current}")


class LoginBrowser:
    """One Camoufox profile kept open between sending the email code and submitting it."""

    def __init__(self, domain: str, email: str, proxy: str, headless: bool, log: Log, cancelled: Callable[[], bool]) -> None:
        self.domain = domain
        self.email = email
        self.cancelled = cancelled
        self.log = log
        self._send_payloads: list[dict[str, Any]] = []
        self._login_payloads: list[dict[str, Any]] = []
        self._watching = False
        self.environment = BrowserEnvironment("", proxy=proxy, headless=headless, log=log)

    def close(self) -> None:
        self.environment.close()

    def send_code(self) -> None:
        self._raise_if_cancelled()
        self.environment.open_url(f"https://{self.domain}/")
        self._watch_responses()
        if not self._wait_for_email(8):
            self._click_text(("登录", "Login", "Sign in"))
            if not self._wait_for_email(8):
                self.environment.open_url(f"https://{self.domain}/login")
                self._watch_responses()
                if not self._wait_for_email(15):
                    raise LoginError("未找到登录页的邮箱输入框")
        if not self._fill_email():
            raise LoginError("未能填入邮箱")
        self._settle_challenge()
        if not self._click_text(("发送验证码", "获取验证码", "发送邮箱验证码", "Send Code", "Get Code", "Send")):
            raise LoginError("未找到发送验证码按钮")
        self._wait_until_code_sent()
        self.log("已发送验证码")

    def submit_code(self, code: str) -> str:
        self._login_payloads.clear()
        self._raise_if_cancelled()
        if not self._fill_code(code):
            raise LoginError("未找到验证码输入框")
        try:
            self._settle_challenge()
        except VerificationError as exc:
            raise CodeRejected("人机验证未通过，请再试一次") from exc
        if not self._click_text(("登录", "登入", "Login", "Sign in")):
            raise LoginError("未找到登录按钮")
        self.log("已提交验证码")

        deadline = time.monotonic() + 45
        last_message = ""
        while time.monotonic() < deadline:
            self._raise_if_cancelled()
            rejection = self._login_rejection()
            if rejection:
                raise CodeRejected(rejection)
            if self._page_says_code_rejected():
                raise CodeRejected("验证码未通过，请再试一次")
            cookie = self.session_cookie()
            if CookieEntry.has_session_field(cookie):
                try:
                    status = self.environment.fetch_current("/api/user/status")
                except (BrowserEnvironmentError, VerificationError) as exc:
                    last_message = str(exc)
                else:
                    body = status.body if isinstance(status.body, dict) else {}
                    if body.get("code") == 0 and isinstance(body.get("data"), dict):
                        return cookie
                    last_message = str(body.get("message") or last_message)
                    if "验证码" in last_message:
                        raise CodeRejected(last_message)
            self._pause(0.5)
        if self._email_visible():
            raise CodeRejected(last_message or "验证码未通过，请再试一次")
        raise LoginError(last_message or "登录后未获得有效会话")

    def session_cookie(self) -> str:
        context = self.environment.context
        if context is None:
            return ""
        found: dict[str, str] = {}
        for item in context.cookies():
            if item.get("name") not in SESSION_NAMES:
                continue
            if not _host_matches(str(item.get("domain") or ""), self.domain):
                continue
            found[str(item["name"])] = str(item.get("value") or "")
        return "; ".join(f"{name}={found[name]}" for name in SESSION_NAMES if found.get(name))

    def _watch_responses(self) -> None:
        page = self.environment.page
        if page is None or self._watching:
            return
        self._watching = True

        def capture(response: Any) -> None:
            try:
                if response.request.method != "POST":
                    return
                url = str(response.url or "").lower()
                if any(part in url for part in ("authorization", "sendmail", "send-code", "mailcode")):
                    bucket = self._send_payloads
                elif "/api/login" in url or "/api/user/login" in url:
                    bucket = self._login_payloads
                else:
                    return
                payload = response.json()
            except Exception:
                return
            if isinstance(payload, dict):
                bucket.append(payload)

        page.on("response", capture)

    def _wait_until_code_sent(self) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self._raise_if_cancelled()
            if self._send_payloads:
                payload = self._send_payloads[-1]
                message = str(payload.get("message") or "")
                code = payload.get("code")
                if code in (0, "0") or any(word in message.lower() for word in ("成功", "已发送", "success", "sent")):
                    return
                if code not in (None, ""):
                    raise LoginError(message or "发送验证码被拒绝")
            if self._countdown_visible():
                return
            self._pause(0.4)
        if self._code_field_present():
            return
        raise LoginError("未确认验证码已发出")

    def _login_rejection(self) -> str:
        if not self._login_payloads:
            return ""
        payload = self._login_payloads[-1]
        code = payload.get("code")
        if code in (0, "0", None, ""):
            return ""
        return str(payload.get("message") or "验证码未通过，请再试一次")

    def _page_says_code_rejected(self) -> bool:
        text = self._body_text().lower()
        return any(word in text for word in ("验证码错误", "验证码不正确", "验证码无效", "invalid code", "code is incorrect"))

    def _fill_email(self) -> bool:
        return bool(self._evaluate(
            """(email) => {
              const visible = (element) => {
                if (!element) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const inputs = [...document.querySelectorAll('input')].filter(visible);
              const input = inputs.find((element) => /email|mail|address|邮/.test(
                `${element.type} ${element.name} ${element.id} ${element.placeholder} ${element.autocomplete}`.toLowerCase()
              ));
              if (!input) return false;
              input.focus();
              const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
              proto.set.call(input, email);
              input.dispatchEvent(new Event('input', {bubbles: true}));
              input.dispatchEvent(new Event('change', {bubbles: true}));
              return true;
            }""",
            self.email,
        ))

    def _fill_code(self, code: str) -> bool:
        return bool(self._evaluate(
            """(code) => {
              const visible = (element) => {
                if (!element) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const inputs = [...document.querySelectorAll('input')].filter(visible);
              const input = inputs.find((element) => /code|mailcode|验证码/.test(
                `${element.name} ${element.id} ${element.placeholder} ${element.getAttribute('aria-label') || ''}`.toLowerCase()
              )) || inputs.find((element) => element.maxLength === 6 || element.maxLength === 4 || element.maxLength === 8);
              if (!input) return false;
              input.focus();
              const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
              proto.set.call(input, code);
              input.dispatchEvent(new Event('input', {bubbles: true}));
              input.dispatchEvent(new Event('change', {bubbles: true}));
              return true;
            }""",
            code,
        ))

    def _click_text(self, labels: tuple[str, ...]) -> bool:
        clicked = self._evaluate(
            """(labels) => {
              const visible = (element) => {
                if (!element) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              const nodes = [...document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]')].filter(visible);
              const textOf = (element) => (element.innerText || element.value || element.getAttribute('aria-label') || '').trim();
              const rank = (element) => element.tagName === 'A' ? 1 : 0;
              const pick = (matcher) => nodes.filter(matcher).sort((left, right) => rank(left) - rank(right))[0];
              for (const label of labels) {
                const needle = String(label).trim().toLowerCase();
                const exact = pick((element) => {
                  const text = textOf(element);
                  return text.length > 0 && text.length <= 40 && text.toLowerCase() === needle;
                });
                if (exact) { exact.click(); return true; }
              }
              for (const label of labels) {
                const needle = String(label).trim().toLowerCase();
                const partial = pick((element) => {
                  const text = textOf(element);
                  return text.length > 0 && text.length <= 40 && text.toLowerCase().includes(needle);
                });
                if (partial) { partial.click(); return true; }
              }
              return false;
            }""",
            list(labels),
        )
        return bool(clicked)

    def _wait_for_email(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._raise_if_cancelled()
            if self._email_visible():
                return True
            self._pause(0.4)
        return False

    def _email_visible(self) -> bool:
        return bool(self._evaluate(
            """() => {
              const visible = (element) => {
                if (!element) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
              };
              return [...document.querySelectorAll('input')].some((element) => visible(element) && /email|mail|address|邮/.test(
                `${element.type} ${element.name} ${element.id} ${element.placeholder} ${element.autocomplete}`.toLowerCase()
              ));
            }"""
        ))

    def _code_field_present(self) -> bool:
        return bool(self._evaluate(
            """() => [...document.querySelectorAll('input')].some((element) => /code|mailcode|验证码/.test(
              `${element.name} ${element.id} ${element.placeholder} ${element.getAttribute('aria-label') || ''}`.toLowerCase()
            ))"""
        ))

    def _countdown_visible(self) -> bool:
        return bool(self._evaluate(
            r"""() => [...document.querySelectorAll('button, a, [role="button"]')].some((element) => /\d+\s*s/i.test((element.innerText || element.value || '').trim()))"""
        ))

    def _body_text(self) -> str:
        try:
            return str(self._evaluate("() => (document.body && document.body.innerText || '').slice(0, 2000)") or "")
        except BrowserEnvironmentError:
            return ""

    def _settle_challenge(self) -> None:
        page = self.environment.page
        if page is None:
            raise BrowserEnvironmentError("浏览器页面未启动")
        self.environment._clear_page_challenge(self.domain)

    def _evaluate(self, script: str, arg: Any = None) -> Any:
        page = self.environment.page
        if page is None:
            raise BrowserEnvironmentError("浏览器页面未启动")
        try:
            if arg is None:
                return page.evaluate(script)
            return page.evaluate(script, arg)
        except Exception as exc:
            raise BrowserEnvironmentError(f"登录页操作失败：{exc}") from exc

    def _pause(self, seconds: float) -> None:
        page = self.environment.page
        if page is None:
            time.sleep(seconds)
            return
        try:
            page.wait_for_timeout(int(seconds * 1000))
        except Exception:
            time.sleep(seconds)

    def _raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise LoginCancelled("已取消")


def start_login(
    domain: str,
    email: str,
    proxies: list[str],
    headless: bool,
    log: Log,
    cancelled: Callable[[], bool],
) -> LoginBrowser:
    """Open a fresh browser and request an email code, trying each proxy in order."""
    last_error = "没有可用的登录环境"
    candidates = proxies or [""]
    for index, proxy in enumerate(candidates, 1):
        if cancelled():
            raise LoginCancelled("已取消")
        browser = LoginBrowser(domain, email, proxy, headless, log, cancelled)
        try:
            log(f"建立登录浏览器（第 {index}/{len(candidates)} 条代理：{proxy_label(proxy)}）")
            browser.send_code()
            return browser
        except LoginCancelled:
            browser.close()
            raise
        except (LoginError, BrowserEnvironmentError, VerificationError) as exc:
            last_error = str(exc)
            log(f"登录环境失败：{exc}")
            browser.close()
    raise LoginError(last_error)
