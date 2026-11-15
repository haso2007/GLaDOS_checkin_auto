"""Local Edge/Chrome fallback for GLaDOS check-in.

The NAS container calls this small service when its own browser cannot obtain a
Cloudflare Turnstile token. The real browser runs on this Windows machine.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from browser_env import _browser_proxy, _cookie_pairs
from turnstile import VerificationError, has_turnstile_widget, read_turnstile_token, wait_for_turnstile


HOST = os.environ.get("GLADOS_LOCAL_BROWSER_HOST", "0.0.0.0")
PORT = int(os.environ.get("GLADOS_LOCAL_BROWSER_PORT", "8792"))
PROFILE = Path(os.environ.get("GLADOS_LOCAL_BROWSER_PROFILE", Path(__file__).resolve().parent / "data" / "local-browser"))
_BROWSER_LOCK = threading.Lock()


def browser_executable() -> Path:
    """Prefer Chrome, then Edge. This machine currently has Edge only."""
    names = ("chrome.exe", "msedge.exe")
    roots = (
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    )
    suffixes = (
        Path("Google/Chrome/Application/chrome.exe"),
        Path("Microsoft/Edge/Application/msedge.exe"),
    )
    found: list[Path] = []
    for root in roots:
        if not root:
            continue
        for suffix in suffixes:
            path = Path(root) / suffix
            if path.exists():
                found.append(path)
    found.sort(key=lambda path: names.index(path.name) if path.name in names else len(names))
    if not found:
        raise FileNotFoundError("未找到 Chrome 或 Edge")
    return found[0]


def _accepted(payload: dict[str, Any]) -> bool:
    message = str(payload.get("message") or "")
    return payload.get("code") in (0, 1) or "cloudflare verification failed" not in message.lower()


def _completed_page_message(page: Any) -> str:
    """Return the displayed completed-state message, without guessing from disabled alone."""
    try:
        text = str(
            page.evaluate(
                """() => {
                  const button = document.querySelector('button.ckb');
                  const body = document.body ? document.body.innerText : '';
                  return [button ? button.className : '', button ? button.innerText : '', body].join('\\n');
                }"""
            )
            or ""
        )
    except Exception:
        return ""
    lowered = text.lower()
    if "today is done" in lowered or "today's observation" in lowered or "already checked in" in lowered:
        return "Today's observation already logged."
    return ""


def _run_page(cookie: str, domain: str, proxy: str) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    executable = browser_executable()
    PROFILE.mkdir(parents=True, exist_ok=True)
    responses: list[dict[str, Any]] = []

    def capture(response: Any) -> None:
        try:
            if response.request.method == "POST" and "/api/user/checkin" in str(response.url):
                payload = response.json()
                if isinstance(payload, dict):
                    responses.append(payload)
        except Exception:
            pass

    with sync_playwright() as playwright:
        options: dict[str, Any] = {
            "user_data_dir": str(PROFILE),
            "executable_path": str(executable),
            "headless": False,
            "ignore_default_args": ["--enable-automation"],
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        browser_proxy = _browser_proxy(proxy)
        if browser_proxy:
            options["proxy"] = browser_proxy
        context = playwright.chromium.launch_persistent_context(**options)
        try:
            context.add_cookies(
                [
                    {"name": name, "value": value, "domain": domain, "path": "/", "secure": True}
                    for name, value in _cookie_pairs(cookie)
                ]
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"https://{domain}/console/checkin", wait_until="domcontentloaded", timeout=60_000)
            page.on("response", capture)
            button = page.locator("button.ckb").first
            button.wait_for(state="visible", timeout=30_000)

            # React initially renders a disabled button while it loads status and
            # the nonce-bound challenge. Do not mistake that short state for a
            # failed check-in. The completed state stays disabled with is-done.
            def completed() -> dict[str, Any] | None:
                message = _completed_page_message(page)
                if not message:
                    return None
                return {
                    "ok": True,
                    "browser": executable.name,
                    "payload": {"code": 1, "repeated": True, "message": message},
                }

            button_deadline = time.monotonic() + 65
            while time.monotonic() < button_deadline:
                done = completed()
                if done:
                    return done
                if button.is_enabled():
                    break
                page.wait_for_timeout(500)
            else:
                raise RuntimeError("本机签到页面加载后按钮仍不可用；请确认 Cookie 是否有效或刷新页面重试")

            # The interaction-only Turnstile layer can overlap the enabled button.
            # Force only bypasses Playwright's hit-test; React still receives the
            # native click and the page, not this code, creates/submits the token.
            button.click(timeout=15_000, force=True)
            deadline = time.monotonic() + 110
            attempts = 0
            while time.monotonic() < deadline:
                if responses and _accepted(responses[-1]):
                    return {"ok": True, "browser": executable.name, "payload": responses[-1]}
                done = completed()
                if done:
                    return done
                if read_turnstile_token(page):
                    responses.clear()
                    button.click(timeout=15_000, force=True)
                    continue
                if attempts < 3 and (has_turnstile_widget(page) or responses):
                    attempts += 1
                    try:
                        wait_for_turnstile(page, timeout=min(40, deadline - time.monotonic()))
                    except VerificationError:
                        if attempts >= 3 and not read_turnstile_token(page):
                            break
                    if read_turnstile_token(page):
                        continue
                    responses.clear()
                    button.click(timeout=15_000, force=True)
                    continue
                page.wait_for_timeout(500)
            if responses:
                return {"ok": _accepted(responses[-1]), "browser": executable.name, "payload": responses[-1]}
            raise VerificationError("本机浏览器未获得签到结果")
        finally:
            context.close()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_error(404)
            return
        try:
            browser = browser_executable().name
        except FileNotFoundError:
            browser = ""
        self._send({"ok": bool(browser), "browser": browser})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/checkin":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            cookie = str(payload.get("cookie") or "")
            domain = str(payload.get("domain") or "").strip()
            proxy = str(payload.get("proxy") or "")
            if not cookie or not domain:
                raise ValueError("缺少 Cookie 或域名")
            with _BROWSER_LOCK:
                result = _run_page(cookie, domain, proxy)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        self._send(result)

    def _send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
