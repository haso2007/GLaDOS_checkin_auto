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
              host.scrollIntoView({block: 'center', inline: 'center'});
              let rect = host.getBoundingClientRect();
              if (rect.top < 0 || rect.bottom > window.innerHeight) {
                window.scrollBy(0, rect.top - (window.innerHeight - rect.height) / 2);
                rect = host.getBoundingClientRect();
              }
              return {x: rect.x, y: rect.y, width: rect.width, height: rect.height, viewWidth: window.innerWidth, viewHeight: window.innerHeight};
            }"""
        )
    except Exception:
        return None
    width = float(box.get("width") or 0) if box else 0
    height = float(box.get("height") or 0) if box else 0
    if width < 250 or width > 340 or height < 50 or height > 90:
        return None
    view_width = float(box.get("viewWidth") or 0)
    view_height = float(box.get("viewHeight") or 0)
    if view_width and (box["x"] < 0 or box["x"] + width > view_width):
        return None
    if view_height and (box["y"] < 0 or box["y"] + height > view_height):
        return None
    return box


def _checkbox_target(page: Any) -> dict[str, float] | None:
    """Return the checkbox point on the Turnstile iframe, not the outer host box."""
    _security_box(page)
    frame = None
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    for item in frames:
        lowered = str(getattr(item, "url", "") or "").lower()
        if "challenges.cloudflare.com" in lowered or "turnstile" in lowered:
            frame = item
            break
    if frame is None:
        return None
    try:
        body = frame.evaluate(
            """() => {
              const node = document.body;
              if (!node) return null;
              const rect = node.getBoundingClientRect();
              return {w: rect.width, h: rect.height};
            }"""
        )
    except Exception:
        return None
    if float((body or {}).get("w") or 0) < 200 or float((body or {}).get("h") or 0) < 40:
        return None
    try:
        box = frame.frame_element().bounding_box()
    except Exception:
        box = None
    if not box or float(box.get("width") or 0) < 200 or float(box.get("height") or 0) < 40:
        return None
    return {
        "x": float(box["x"]) + 24,
        "y": float(box["y"]) + float(box["height"]) / 2,
        "left": float(box["x"]),
        "top": float(box["y"]),
        "width": float(box["width"]),
        "height": float(box["height"]),
    }


def _click_visible_security(page: Any, log: Log | None = None, target: dict[str, float] | None = None) -> bool:
    """Click the checkbox inside the visible Turnstile iframe."""
    point = target or _checkbox_target(page)
    if not point:
        if log:
            log("Security 控件还没出现在画面里")
        return False
    point_x = float(point["x"])
    point_y = float(point["y"])
    page.mouse.move(point_x + 80, point_y + 30)
    time.sleep(0.2)
    page.mouse.move(point_x, point_y)
    time.sleep(0.15)
    page.mouse.down()
    time.sleep(0.08)
    page.mouse.up()
    if log:
        log(
            "已点击 Turnstile 复选框 "
            f"({point_x:.0f}, {point_y:.0f})，"
            f"框 ({float(point['left']):.0f}, {float(point['top']):.0f}) "
            f"{float(point['width']):.0f}x{float(point['height']):.0f}"
        )
    return True


def _click_security_checkbox(page: Any) -> bool:
    return _click_visible_security(page)


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


def _scroll_security_into_view(page: Any) -> None:
    try:
        page.evaluate(
            """() => {
              const button = document.querySelector('button.ckb');
              const host = button && button.previousElementSibling;
              if (host) host.scrollIntoView({block: 'center', inline: 'center'});
            }"""
        )
    except Exception:
        return


def _click_turnstile_frame(page: Any, log: Log | None = None) -> bool:
    """Click the Turnstile checkbox inside its frame, then fall back to the iframe box."""
    _scroll_security_into_view(page)
    frame = None
    frame_urls: list[str] = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    for item in frames:
        url = str(getattr(item, "url", "") or "")
        frame_urls.append(url[:80])
        lowered = url.lower()
        if "challenges.cloudflare.com" in lowered or "turnstile" in lowered:
            frame = item
            break
    if frame is None:
        if log:
            log(f"Turnstile frame 未找到，当前 frames({len(frame_urls)}): {frame_urls}")
        return False
    if log:
        log(f"Turnstile frame 已定位: {str(frame.url or '')[:100]}")
    try:
        body = frame.evaluate(
            """() => {
              const node = document.body;
              if (!node) return null;
              const rect = node.getBoundingClientRect();
              return {w: rect.width, h: rect.height};
            }"""
        )
        width = float((body or {}).get("w") or 0)
        height = float((body or {}).get("h") or 0)
        if log:
            log(f"Turnstile frame body: w={width:.0f} h={height:.0f}")
        if width > 0 and height > 0:
            click_x = 24
            click_y = height / 2
            frame.click("body", position={"x": click_x, "y": click_y}, timeout=3000)
            if log:
                log(f"已点击 Turnstile frame body ({click_x}, {click_y:.0f})")
            return True
        if log:
            log("Turnstile frame body 未渲染好")
    except Exception as exc:
        if log:
            log(f"Turnstile frame body 点击失败: {exc}")
    try:
        iframe = page.query_selector('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]')
        box = iframe.bounding_box() if iframe else None
        if box and float(box.get("width") or 0) > 0:
            point_x = float(box["x"]) + 24
            point_y = float(box["y"]) + float(box["height"]) / 2
            page.mouse.click(point_x, point_y)
            if log:
                log(f"已在页面坐标点击 Turnstile iframe ({point_x:.0f}, {point_y:.0f})")
            return True
    except Exception as exc:
        if log:
            log(f"页面坐标点击失败: {exc}")
    return False


def wait_for_turnstile(page: Any, *, timeout: float = 40.0, log: Log | None = None, method_index: int = 0) -> str:
    """Poll the widget and click its frame again in this same browser until a token appears."""
    del method_index
    rounds = max(8, int(max(timeout, 5.0) / 2))
    seen_at: int | None = None
    clicked = False
    last_click = -100
    for index in range(rounds):
        token = read_turnstile_token(page)
        if token:
            if log:
                log(f"Cloudflare Turnstile 已通过（token 长度 {len(token)}）")
            return token
        target = _checkbox_target(page)
        if target is None:
            seen_at = None
            if not clicked or index - last_click >= 6:
                if log:
                    log("Security 控件还没出现在画面里")
            try:
                page.wait_for_timeout(2000)
            except Exception:
                time.sleep(2)
            continue
        if seen_at is None:
            seen_at = index
            if log:
                log(
                    "Security 已出现，"
                    f"框 ({target['left']:.0f}, {target['top']:.0f}) "
                    f"{target['width']:.0f}x{target['height']:.0f}，停稳后再点"
                )
            try:
                page.wait_for_timeout(2000)
            except Exception:
                time.sleep(2)
            continue
        if not clicked or index - last_click >= 6:
            if log:
                log("点击 Turnstile 复选框" if not clicked else "再次点击 Turnstile 复选框")
            _click_visible_security(page, log, target)
            clicked = True
            last_click = index
        try:
            page.wait_for_timeout(2000)
        except Exception:
            time.sleep(2)
    raise VerificationError("Cloudflare Turnstile 未在超时时间内通过")
