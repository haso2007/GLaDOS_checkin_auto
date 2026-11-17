"""One browser cache directory per account.

A new account gets its own directory. Login and check-in never read another
account's cookies, storage, or Chromium profile.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def state_dir(data_dir: Path, account_id: str) -> Path:
    safe = "".join(char for char in str(account_id) if char.isalnum() or char in "-_")
    if not safe:
        raise ValueError("账号 ID 为空，无法分配独立缓存")
    return Path(data_dir) / "browser-state" / safe


def ensure_account_states(data_dir: Path, account_ids: set[str]) -> None:
    """Create a directory for every current account and remove caches that no longer have one."""
    root = Path(data_dir) / "browser-state"
    root.mkdir(parents=True, exist_ok=True)
    live = set()
    for account_id in account_ids:
        path = state_dir(data_dir, account_id)
        path.mkdir(parents=True, exist_ok=True)
        live.add(path.name)
    for child in root.iterdir():
        if child.is_dir() and child.name not in live:
            shutil.rmtree(child, ignore_errors=True)


def cache_ready(data_dir: Path, account_id: str) -> bool:
    path = state_dir(data_dir, account_id)
    return (path / "user-data-dir").is_dir() or (path / "storage_state.json").is_file()


def _ignore_profile_noise(_directory: str, names: list[str]) -> set[str]:
    skipped = {"SingletonLock", "SingletonCookie", "SingletonSocket", "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache", "component_crx_cache"}
    return {name for name in names if name in skipped or name.startswith("BrowserMetrics")}


def save_browser_state(environment: Any, directory: Path) -> None:
    """Persist this browser's cookies and profile. Page storage stays inside the profile."""
    context = environment.context
    page = environment.page
    profile = environment.profile
    if context is None or page is None:
        raise RuntimeError("浏览器未打开，无法保存账号缓存")
    directory.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(directory / "storage_state.json"))
    if profile is not None and Path(profile).is_dir():
        target = directory / "user-data-dir"
        if Path(profile).resolve() != target.resolve():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(profile, target, ignore=_ignore_profile_noise)


def saved_cookies(directory: Path, domain: str) -> list[dict[str, Any]]:
    path = directory / "storage_state.json"
    if not path.is_file():
        return []
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        item for item in state.get("cookies") or []
        if isinstance(item, dict) and domain in str(item.get("domain") or "")
    ]


def profile_source(directory: Path) -> Path | None:
    path = directory / "user-data-dir"
    return path if path.is_dir() else None
