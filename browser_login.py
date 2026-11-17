"""Log in through the same browser session that will later check in.

The GLaDOS page issues the session to the browser that submits the email code.
This module opens the login page, fills the email, sends the code, then pauses
until the operator submits the mailbox code. It does not forge a Turnstile token
and it does not keep the verification code.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from browser_env import BrowserEnvironment, BrowserEnvironmentError, proxy_label
from glados import CookieEntry
from turnstile import VerificationError


Log = Callable[[str], None]

SESSION_NAMES = ("koa:sess", "koa:sess.sig", "gld:sess", "gld:sess.sig")
CODE_WAIT_SECONDS = 180
_SEND_LABELS = ("发送验证码", "获取验证码", "发送邮箱验证码", "Send Code", "Get Code")
_LOGIN_LABELS = ("登录", "登入", "Login", "Sign in")


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

    def __init__(self, domain: str, email: str, proxy: str, headless: bool, log: Log, cancelled: Callable[[], bool], state_dir: Any = None) -> None:
        self.domain = domain
        self.email = email
        self.cancelled = cancelled
        self.log = log
        self._send_payloads: list[dict[str, Any]] = []
        self._login_payloads: list[dict[str, Any]] = []
        self._phase = "send"
        self._watching = False
        self.environment = BrowserEnvironment("", proxy=proxy, headless=headless, log=log, state_dir=state_dir)

    def close(self) -> None:
        self.environment.close()

    def send_code(self) -> None:
        self._raise_if_cancelled()
        opened = False
        for url in (f"https://{self.domain}/login", f"https://{self.domain}/"):
            self.log(f"打开登录页 {url}")
            self.environment.open_url(url)
            self._watch_responses()
            page = self._page()
            self.log(f"当前页面 {page.url or url}")
            if self._email_box(2) is not None:
                opened = True
                break
            if self._already_inside():
                self._drop_site_session()
                self.log("重新打开登录页")
                self.environment.open_url(f"https://{self.domain}/login")
                self.log(f"当前页面 {self._page().url}")
                if self._email_box(8) is not None:
                    opened = True
                    break
            self.log("这一页没有邮箱框，尝试点开登录入口")
            if self._click_labels(_LOGIN_LABELS) and self._email_box(8) is not None:
                opened = True
                break
        if not opened:
            raise LoginError("未找到登录页的邮箱输入框")
        email_box = self._email_box(1)
        if email_box is None:
            raise LoginError("未找到登录页的邮箱输入框")
        self.log(f"填写邮箱 {self.email}")
        self._fill(email_box, self.email)
        try:
            self._settle_challenge()
            self.log("登录页人机验证已处理")
        except VerificationError as exc:
            self.log(f"登录页人机验证未通过：{exc}")
            raise
        mark = len(self._send_payloads)
        if not self._click_labels(_SEND_LABELS):
            raise LoginError("未找到发送验证码按钮")
        self.log("已点击发送验证码，等待接口返回")
        self._wait_until_code_sent(mark)
        self.log("验证码已发出。登录暂停，等待你在页面输入邮箱验证码")

    def submit_code(self, code: str) -> str:
        self._login_payloads.clear()
        self._phase = "login"
        self._raise_if_cancelled()
        page = self._page()
        self.log(f"继续登录，当前页面 {page.url}")
        code_box = self._code_box()
        if code_box is None:
            raise LoginError("未找到验证码输入框")
        self.log("填写邮箱验证码")
        self._fill(code_box, code)
        try:
            self._settle_challenge()
        except VerificationError as exc:
            raise CodeRejected("人机验证未通过，请再试一次") from exc
        mark = len(self._login_payloads)
        if not self._click_labels(_LOGIN_LABELS):
            raise LoginError("未找到登录按钮")
        self.log("已点击登录，等待登录接口")

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
                self.log("浏览器已拿到会话 Cookie，核对账户状态")
                try:
                    status = self.environment.fetch_current("/api/user/status")
                except (BrowserEnvironmentError, VerificationError) as exc:
                    last_message = str(exc)
                    self.log(f"核对账户状态失败：{exc}")
                else:
                    body = status.body if isinstance(status.body, dict) else {}
                    if body.get("code") == 0 and isinstance(body.get("data"), dict):
                        self.log("登录通过，会话已写入当前浏览器")
                        return cookie
                    last_message = str(body.get("message") or last_message)
                    if "验证码" in last_message:
                        raise CodeRejected(last_message)
            self._pause(0.5)
        if self._email_box(0.5) is not None:
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
                url = str(response.url or "")
                lowered = url.lower()
                if self._phase == "login" and (
                    "/api/login" in lowered
                    or "/api/user/login" in lowered
                    or "authorization" in lowered
                    or lowered.rstrip("/").endswith("/login")
                ):
                    bucket = self._login_payloads
                    kind = "登录"
                elif any(part in lowered for part in ("authorization", "sendmail", "send-code", "mailcode", "send_code")):
                    bucket = self._send_payloads
                    kind = "发送验证码"
                elif "/api/login" in lowered or "/api/user/login" in lowered or lowered.rstrip("/").endswith("/login"):
                    bucket = self._login_payloads
                    kind = "登录"
                else:
                    return
                payload = response.json()
            except Exception:
                return
            if not isinstance(payload, dict):
                return
            bucket.append(payload)
            message = str(payload.get("message") or payload.get("msg") or "")
            self.log(f"{kind}接口 {response.status} {url} code={payload.get('code')} {message}".strip())

        page.on("response", capture)

    def _wait_until_code_sent(self, mark: int) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self._raise_if_cancelled()
            fresh = self._send_payloads[mark:]
            if fresh:
                payload = fresh[-1]
                message = str(payload.get("message") or payload.get("msg") or "")
                code = payload.get("code")
                if code in (0, "0") or any(word in message.lower() for word in ("成功", "已发送", "success", "sent")):
                    self.log(f"发送验证码成功：{message or 'code=0'}")
                    return
                if code not in (None, ""):
                    raise LoginError(message or "发送验证码被拒绝")
            if self._countdown_visible():
                self.log("发送按钮已进入倒计时，视为验证码已发出")
                return
            self._pause(0.4)
        if self._code_box() is not None:
            self.log("验证码输入框已出现，暂停等待手动输入")
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

    def _page(self) -> Any:
        page = self.environment.page
        if page is None:
            raise BrowserEnvironmentError("浏览器页面未启动")
        return page

    def _fill(self, locator: Any, text: str) -> None:
        locator.fill(text, timeout=8000)
        self._pause(0.2)

    def _click_labels(self, labels: tuple[str, ...]) -> bool:
        page = self._page()
        pattern = re.compile("|".join(re.escape(label) for label in labels), re.IGNORECASE)
        for role in ("button", "link"):
            locator = page.get_by_role(role, name=pattern)
            try:
                count = locator.count()
            except Exception:
                count = 0
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not item.is_visible():
                        continue
                    label = " ".join((item.inner_text(timeout=1000) or "").split())
                    if len(label) > 40:
                        continue
                    self.log(f"点击「{label or labels[0]}」")
                    item.click(timeout=8000)
                    self._pause(0.4)
                    return True
                except Exception as exc:
                    self.log(f"点击失败，换下一个控件：{exc}")
        return False

    def _already_inside(self) -> bool:
        url = str(self._page().url or "")
        return "/console" in url or "/dashboard" in url

    def _drop_site_session(self) -> None:
        context = self.environment.context
        if context is None:
            return
        self.log("当前已登录，重新登录前清除该站点 Cookie")
        context.clear_cookies()

    def _email_box(self, timeout: float) -> Any | None:
        page = self._page()
        locator = page.locator(
            "input[type='email'], input[name*='mail' i], input[id*='mail' i], "
            "input[placeholder*='邮'], input[placeholder*='mail' i], input[autocomplete='email']"
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._raise_if_cancelled()
            try:
                count = locator.count()
            except Exception:
                count = 0
            for index in range(count):
                item = locator.nth(index)
                try:
                    if item.is_visible():
                        return item
                except Exception:
                    continue
            self._pause(0.4)
        return None

    def _code_box(self) -> Any | None:
        page = self._page()
        locator = page.locator(
            "input[name*='code' i], input[id*='code' i], input[placeholder*='验证码'], "
            "input[placeholder*='code' i], input[maxlength='6'], input[maxlength='4'], input[maxlength='8']"
        )
        try:
            count = locator.count()
        except Exception:
            return None
        for index in range(count):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    return item
            except Exception:
                continue
        return None

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
    state_dir: Any = None,
) -> LoginBrowser:
    """Open a fresh browser and request an email code, trying each proxy in order."""
    last_error = "没有可用的登录环境"
    candidates = proxies or [""]
    for index, proxy in enumerate(candidates, 1):
        if cancelled():
            raise LoginCancelled("已取消")
        browser = LoginBrowser(domain, email, proxy, headless, log, cancelled, state_dir)
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
