"""Cloudflare Turnstile helpers used by isolated Chromium browser contexts.

The token is always issued by the widget loaded in the current browser page.  This
module deliberately does not forge a token or invoke ``turnstile.reset()``.
"""

from __future__ import annotations

import random
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


def _human_click(page: Any, x: float, y: float) -> None:
    """Move onto the control, then press and release, the same gesture as a slider start."""
    start_x = x + random.uniform(36, 110)
    start_y = y + random.uniform(-28, 36)
    page.mouse.move(start_x, start_y)
    time.sleep(random.uniform(0.08, 0.16))
    steps = random.randint(7, 12)
    for index in range(1, steps + 1):
        page.mouse.move(
            start_x + (x - start_x) * index / steps + random.uniform(-0.8, 0.8),
            start_y + (y - start_y) * index / steps + random.uniform(-0.8, 0.8),
        )
        time.sleep(random.uniform(0.008, 0.02))
    time.sleep(random.uniform(0.05, 0.12))
    page.mouse.down()
    time.sleep(random.uniform(0.04, 0.09))
    page.mouse.up()


def _security_box(page: Any) -> dict[str, float] | None:
    """The live widget is the 300x68 box immediately before the Checkin button."""
    try:
        box = page.evaluate(
            """() => {
              const button = document.querySelector('button.ckb');
              const host = button && button.previousElementSibling;
              if (!host) return null;
              host.scrollIntoView({block: 'center', inline: 'nearest'});
              const rect = host.getBoundingClientRect();
              return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
            }"""
        )
    except Exception:
        return None
    width = float(box.get("width") or 0) if box else 0
    height = float(box.get("height") or 0) if box else 0
    if width < 250 or width > 340 or height < 50 or height > 90:
        return None
    return box


def _click_security_checkbox(page: Any) -> bool:
    box = _security_box(page)
    if not box:
        return False
    _human_click(page, box["x"] + min(28, box["width"] / 5), box["y"] + box["height"] / 2)
    return True


def _click_security_center(page: Any) -> bool:
    box = _security_box(page)
    if not box:
        return False
    _human_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    return True


def _click_frame_checkbox(page: Any, *, force: bool) -> bool:
    for frame in _turnstile_frames(page):
        try:
            frame.evaluate(_PATCH_MOUSE_JS)
            locator = frame.locator("input[type='checkbox'], .mark, [role='checkbox']")
            if locator.count():
                locator.first.click(timeout=2500, force=force)
                return True
        except Exception:
            continue
    return False


def _click_turnstile_container(page: Any) -> bool:
    try:
        container = page.query_selector("#turnstileBox, .cf-turnstile, [data-sitekey]")
        box = container.bounding_box() if container else None
        if box and float(box.get("width") or 0) >= 80:
            _human_click(page, box["x"] + 26, box["y"] + box["height"] / 2)
            return True
    except Exception:
        return False
    return False


def _click_frame_body(page: Any) -> bool:
    for frame in _turnstile_frames(page):
        try:
            size = frame.evaluate(
                """() => { const r = document.body && document.body.getBoundingClientRect();
                return r ? {width: r.width, height: r.height} : null; }"""
            )
            if size and float(size.get("width") or 0) >= 40:
                frame.evaluate(_PATCH_MOUSE_JS)
                frame.click("body", position={"x": 24, "y": float(size.get("height") or 65) / 2}, timeout=3000)
                return True
        except Exception:
            continue
    return False


def _click_iframe_box(page: Any) -> bool:
    try:
        iframe = page.query_selector('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]')
        box = iframe.bounding_box() if iframe else None
        if box and float(box.get("width") or 0) >= 40:
            page.mouse.click(box["x"] + 24, box["y"] + box["height"] / 2)
            return True
    except Exception:
        return False
    return False


_SECURITY_METHODS: tuple[tuple[str, Callable[[Any], bool]], ...] = (
    ("鼠标点 Security 复选框", _click_security_checkbox),
    ("鼠标点 Security 中心", _click_security_center),
    ("点击框内 checkbox", lambda page: _click_frame_checkbox(page, force=False)),
    ("强制点击框内 checkbox", lambda page: _click_frame_checkbox(page, force=True)),
    ("鼠标点 Turnstile 容器", _click_turnstile_container),
    ("点击 Turnstile frame", _click_frame_body),
    ("点击 Turnstile iframe 坐标", _click_iframe_box),
)


def security_method_count() -> int:
    return len(_SECURITY_METHODS)


def security_method_name(method_index: int) -> str:
    return _SECURITY_METHODS[method_index % len(_SECURITY_METHODS)][0]


def click_turnstile_widget(page: Any, log: Log | None = None, method_index: int = 0) -> bool:
    """Try one Security click method. The caller rotates the index across attempts."""
    name, method = _SECURITY_METHODS[method_index % len(_SECURITY_METHODS)]
    try:
        clicked = bool(method(page))
    except Exception:
        clicked = False
    if log:
        log(f"{'已尝试' if clicked else '未能使用'}：{name}")
    return clicked


def wait_for_turnstile(page: Any, *, timeout: float = 40.0, log: Log | None = None, method_index: int = 0) -> str:
    """Wait for one Security click in this browser. A failure needs a new browser."""
    deadline = time.monotonic() + max(timeout, 5.0)
    auto_wait_until = time.monotonic() + 3.0
    clicked = False
    name = _SECURITY_METHODS[method_index % len(_SECURITY_METHODS)][0]

    while time.monotonic() < deadline:
        token = read_turnstile_token(page)
        if token:
            if log:
                log(f"Cloudflare Turnstile 已通过（token 长度 {len(token)}）")
            return token

        now = time.monotonic()
        if not clicked and now >= auto_wait_until and has_turnstile_widget(page):
            if log:
                log(f"本浏览器只试一次：{name}")
            click_turnstile_widget(page, log, method_index)
            clicked = True
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
