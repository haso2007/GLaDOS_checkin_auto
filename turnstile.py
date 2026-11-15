"""Cloudflare Turnstile helpers used by the isolated Camoufox browser.

The token is always issued by the widget loaded in the current browser page.  This
module deliberately does not forge a token or invoke ``turnstile.reset()``.
"""

from __future__ import annotations

import time
from typing import Any, Callable


Log = Callable[[str], None]
TOKEN_MIN_LENGTH = 80


class VerificationError(RuntimeError):
    """The browser could not clear a Cloudflare verification challenge."""


_READ_TOKEN_JS = """() => {
  try {
    const selectors = [
      'textarea[name="cf-turnstile-response"]',
      'input[name="cf-turnstile-response"]',
      '[name="turnstile-token"]',
    ];
    for (const selector of selectors) {
      const value = String((document.querySelector(selector) || {}).value || '').trim();
      if (value) return value;
    }
    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
      return String(window.turnstile.getResponse() || '').trim();
    }
  } catch (_) {}
  return '';
}"""

_PATCH_MOUSE_JS = """() => {
  try { window.dtp = 1; } catch (_) {}
  try {
    const sx = 800 + Math.floor(Math.random() * 400);
    const sy = 400 + Math.floor(Math.random() * 300);
    Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx, configurable: true });
    Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy, configurable: true });
  } catch (_) {}
}"""


def read_turnstile_token(page: Any) -> str:
    """Return a real token currently held by the page widget, if any."""
    try:
        token = str(page.evaluate(_READ_TOKEN_JS) or "").strip()
    except Exception:
        return ""
    return token if len(token) >= TOKEN_MIN_LENGTH else ""


def has_turnstile_widget(page: Any) -> bool:
    """Detect an explicit widget, including widgets hosted in Cloudflare frames."""
    try:
        for frame in page.frames:
            url = str(frame.url or "").lower()
            if "challenges.cloudflare.com" in url or "turnstile" in url:
                return True
        return bool(
            page.query_selector(
                "#turnstileBox, .cf-turnstile, [data-sitekey], "
                "[name='cf-turnstile-response'], [name='turnstile-token']"
            )
        )
    except Exception:
        return False


def _turnstile_frames(page: Any) -> list[Any]:
    try:
        return [
            frame
            for frame in page.frames
            if "challenges.cloudflare.com" in str(frame.url or "").lower()
            or "turnstile" in str(frame.url or "").lower()
        ]
    except Exception:
        return []


def click_turnstile_widget(page: Any, log: Log | None = None) -> bool:
    """Click the actual Cloudflare control using the Proxy001 proven order."""
    try:
        box = page.evaluate(
            """() => {
              const button = document.querySelector('button.ckb');
              const host = button && button.previousElementSibling;
              const iframe = host && host.querySelector('iframe');
              const target = iframe || host;
              if (!target) return null;
              const rect = target.getBoundingClientRect();
              return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
            }"""
        )
        if box and float(box.get("width") or 0) >= 20 and float(box.get("height") or 0) >= 20:
            page.mouse.click(box["x"] + min(26, box["width"] / 2), box["y"] + box["height"] / 2)
            if log:
                log("已点击签到按钮旁的 Cloudflare Turnstile")
            return True
    except Exception:
        pass
    for frame in _turnstile_frames(page):
        try:
            frame.evaluate(_PATCH_MOUSE_JS)
            locator = frame.locator("input[type='checkbox'], input, .mark, [role='checkbox']")
            if locator.count():
                locator.first.click(timeout=2500, force=True)
                if log:
                    log("已点击 Cloudflare Turnstile checkbox")
                return True
        except Exception:
            pass

    try:
        container = page.query_selector("#turnstileBox, .cf-turnstile, [data-sitekey]")
        box = container.bounding_box() if container else None
        if box and float(box.get("width") or 0) >= 120:
            page.mouse.click(box["x"] + 26, box["y"] + box["height"] / 2)
            if log:
                log("已点击 Cloudflare Turnstile 容器")
            return True
    except Exception:
        pass

    for frame in _turnstile_frames(page):
        try:
            size = frame.evaluate(
                """() => { const r = document.body && document.body.getBoundingClientRect();
                return r ? {width: r.width, height: r.height} : null; }"""
            )
            if size and float(size.get("width") or 0) >= 120:
                frame.evaluate(_PATCH_MOUSE_JS)
                frame.click("body", position={"x": 24, "y": float(size.get("height") or 65) / 2}, timeout=3000)
                if log:
                    log("已点击 Cloudflare Turnstile frame")
                return True
        except Exception:
            pass

    try:
        iframe = page.query_selector('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]')
        box = iframe.bounding_box() if iframe else None
        if box and float(box.get("width") or 0) >= 120:
            page.mouse.click(box["x"] + 24, box["y"] + box["height"] / 2)
            if log:
                log("已点击 Cloudflare Turnstile iframe")
            return True
    except Exception:
        pass
    return False


def wait_for_turnstile(page: Any, *, timeout: float = 40.0, log: Log | None = None) -> str:
    """Wait for automatic verification, then click a rendered widget if needed."""
    deadline = time.monotonic() + max(timeout, 5.0)
    auto_wait_until = time.monotonic() + 3.0
    last_click = 0.0

    while time.monotonic() < deadline:
        token = read_turnstile_token(page)
        if token:
            if log:
                log(f"Cloudflare Turnstile 已通过（token 长度 {len(token)}）")
            return token

        now = time.monotonic()
        if now >= auto_wait_until and now - last_click >= 6.0 and has_turnstile_widget(page):
            if log:
                log("尝试点击 Cloudflare Turnstile")
            click_turnstile_widget(page, log)
            last_click = now
            try:
                page.wait_for_timeout(1500)
            except Exception:
                time.sleep(1.5)
            continue

        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)

    raise VerificationError("Cloudflare Turnstile 未在超时时间内通过")
