"""Persistent, local-only configuration for the GLaDOS browser service."""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "accounts": [],
    "proxies_text": "",
    "auto_exchange": True,
    "auto_exchange_left_days": 1,
    "verbose": False,
    "browser_headless": False,
    "browser_local": False,
    "local_browser_use_account_proxy": False,
    "base_urls": "",
    "scheduler": {"enabled": False, "time": "01:10", "timezone": "Asia/Shanghai"},
}


class ConfigStore:
    """Serializes configuration writes and migrates the former secret files once."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.data_dir = Path(data_dir or os.environ.get("GLADOS_DATA_DIR", "data"))
        self.path = self.data_dir / "config.json"
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    raw = {}
            else:
                raw = self._bootstrap()
                self._write(raw)
            return copy.deepcopy(self._normalize(raw))

    def save(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            normalized = self._normalize(raw)
            self._write(normalized)
            return copy.deepcopy(normalized)

    def _write(self, data: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def _bootstrap(self) -> dict[str, Any]:
        secrets_dir = Path(os.environ.get("GLADOS_SECRETS_DIR", "secret"))
        cookie_raw = (
            os.environ.get("GLADOS_COOKIES")
            or os.environ.get("GLADOS_COOKIE")
            # Prefer the structured account file when both local files exist.
            or self._read_optional(secrets_dir / "cookie_json")
            or self._read_optional(secrets_dir / "cookie")
        )
        proxies = os.environ.get("GLADOS_PROXIES") or self._read_optional(secrets_dir / "proxy")
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["accounts"] = self._accounts_from_raw(cookie_raw)
        config["proxies_text"] = proxies
        return config

    @staticmethod
    def _read_optional(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @staticmethod
    def _accounts_from_raw(raw: str) -> list[dict[str, Any]]:
        """Import JSON, legacy cookies, or an accidental legacy+JSON paste.

        A common migration path is pasting a JSON account list below an old
        single-cookie line. Preserve both pieces on first import rather than
        treating the whole file as one invalid legacy cookie.
        """
        text = str(raw or "").lstrip("﻿").strip()
        if not text:
            return []

        source: list[dict[str, Any]] = []
        decoder = json.JSONDecoder()
        json_start = next((position for position, char in enumerate(text) if char in "[{") , -1)
        if json_start >= 0:
            try:
                parsed, end = decoder.raw_decode(text[json_start:])
                if not text[json_start + end :].strip():
                    values = parsed if isinstance(parsed, list) else [parsed]
                    source.extend(value for value in values if isinstance(value, dict))
                    legacy = text[:json_start].strip()
                    if legacy:
                        source = [{"cookie": cookie.strip()} for cookie in legacy.split("&") if cookie.strip()] + source
                    return [ConfigStore._normalize_account(value) for value in source]
            except json.JSONDecodeError:
                pass

        source = [{"cookie": cookie.strip()} for cookie in text.split("&") if cookie.strip()]
        return [ConfigStore._normalize_account(value) for value in source]

    @staticmethod
    def _normalize_account(value: Any) -> dict[str, Any]:
        item = value if isinstance(value, dict) else {}
        return {
            "id": str(item.get("id") or uuid.uuid4()),
            "enabled": bool(item.get("enabled", True)),
            "name": str(item.get("name") or "").strip(),
            "origin": str(item.get("origin") or "").strip(),
            "cookie": str(item.get("cookie") or "").strip(),
            "user_agent": " ".join(str(item.get("user_agent") or item.get("user-agent") or "").split()),
            "proxy": str(item.get("proxy") or "").strip(),
        }

    def _normalize(self, raw: Any) -> dict[str, Any]:
        source = raw if isinstance(raw, dict) else {}
        result = copy.deepcopy(DEFAULT_CONFIG)
        result["accounts"] = [self._normalize_account(item) for item in source.get("accounts", []) if isinstance(item, dict)]
        result["proxies_text"] = str(source.get("proxies_text") or "")
        result["auto_exchange"] = bool(source.get("auto_exchange", True))
        try:
            result["auto_exchange_left_days"] = max(0, float(source.get("auto_exchange_left_days", 1)))
        except (TypeError, ValueError):
            result["auto_exchange_left_days"] = 1
        result["verbose"] = bool(source.get("verbose", False))
        result["browser_headless"] = bool(source.get("browser_headless", False))
        result["browser_local"] = bool(source.get("browser_local", False))
        result["local_browser_use_account_proxy"] = bool(source.get("local_browser_use_account_proxy", False))
        result["base_urls"] = str(source.get("base_urls") or "")

        scheduler = source.get("scheduler") if isinstance(source.get("scheduler"), dict) else {}
        time_value = str(scheduler.get("time") or "01:10").strip()
        if len(time_value) != 5 or time_value[2] != ":" or not time_value.replace(":", "").isdigit():
            time_value = "01:10"
        hour, minute = (int(part) for part in time_value.split(":"))
        if hour > 23 or minute > 59:
            time_value = "01:10"
        result["scheduler"] = {
            "enabled": bool(scheduler.get("enabled", False)),
            "time": time_value,
            "timezone": "Asia/Shanghai",
        }
        return result
