"""Focused no-network tests for the native Debian Chromium runtime."""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from browser_env import BrowserEnvironment, _browser_proxy, normalize_proxy
from config_store import ConfigStore
from web_service import JobManager, RuntimeStateStore, Scheduler


class NativeRuntimeTests(unittest.TestCase):
    def test_missing_legacy_browser_mode_defaults_to_headless(self) -> None:
        store = ConfigStore(Path(tempfile.mkdtemp()))
        self.assertEqual(store._normalize({})["browser_mode"], "headless")
        self.assertEqual(store._normalize({"browser_headless": False})["browser_mode"], "headed-xvfb")
        self.assertEqual(store._normalize({"browser_headless": True})["browser_mode"], "headless")

    def test_authenticated_socks_proxy_is_accepted(self) -> None:
        self.assertEqual(normalize_proxy("socks5://user:password@127.0.0.1:1080"), "socks5://user:password@127.0.0.1:1080")
        self.assertEqual(normalize_proxy("socks5h://127.0.0.1:1080"), "socks5://127.0.0.1:1080")
        self.assertEqual(
            _browser_proxy("socks5://user:password@127.0.0.1:1080"),
            {"server": "socks5://127.0.0.1:1080", "username": "user", "password": "password"},
        )

    def test_account_user_agent_reaches_persistent_chromium_context(self) -> None:
        captured: dict[str, object] = {}

        class FakeContext:
            pages: list[object] = []

            def set_default_timeout(self, value: int) -> None:
                del value

            def set_default_navigation_timeout(self, value: int) -> None:
                del value

            def new_page(self) -> object:
                return object()

            def close(self) -> None:
                pass

        class FakeBrowser:
            contexts = [FakeContext()]

            def close(self) -> None:
                pass

        class FakeChromium:
            def connect_over_cdp(self, endpoint: str) -> FakeBrowser:
                captured["endpoint"] = endpoint
                return FakeBrowser()

        class FakeManager:
            chromium = FakeChromium()

            def stop(self) -> None:
                pass

        class FakeStarter:
            def start(self) -> FakeManager:
                return FakeManager()

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "chromium"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            sync_api = types.ModuleType("playwright.sync_api")
            sync_api.sync_playwright = lambda: FakeStarter()
            package = types.ModuleType("playwright")
            with patch.dict(
                sys.modules,
                {"playwright": package, "playwright.sync_api": sync_api},
            ), patch.dict(
                os.environ,
                {"GLADOS_CHROMIUM_EXECUTABLE": str(executable), "GLADOS_BROWSER_PROFILE_DIR": directory},
                clear=False,
            ), patch("browser_env.probe_exit_ip", return_value=("203.0.113.1", "")), patch(
                "browser_env.subprocess.Popen",
                side_effect=lambda command, **kwargs: captured.update({"command": command}) or types.SimpleNamespace(pid=1, poll=lambda: None, wait=lambda timeout=0: 0, terminate=lambda: None, kill=lambda: None),
            ), patch("browser_env._wait_for_cdp"), patch("browser_env.os.killpg"):
                environment = BrowserEnvironment("gld:sess=value", user_agent="Agent/1.0")
                environment.open()
                environment.close()

        command = " ".join(str(part) for part in captured["command"])
        self.assertNotIn("--user-agent=", command)
        self.assertIn("--headless=new", command)
        self.assertIn("--user-data-dir=", command)
        self.assertTrue(str(captured["endpoint"]).startswith("http://127.0.0.1:"))

    def test_job_results_are_restored_for_retry_after_restart(self) -> None:
        data_dir = Path(tempfile.mkdtemp())
        first = JobManager(ConfigStore(data_dir))
        with first._lock:
            first._state.update(
                {
                    "reason": "手动",
                    "started_at": "2026-11-15T01:00:00+08:00",
                    "finished_at": "2026-11-15T01:01:00+08:00",
                    "results": [{"account_id": "account-a", "status": "failure"}],
                }
            )
            first._persist_locked()

        restored = JobManager(ConfigStore(data_dir))
        self.assertFalse(restored.snapshot()["running"])
        self.assertEqual(restored.snapshot()["results"], [{"account_id": "account-a", "status": "failure"}])

    def test_interrupted_job_is_marked_complete_on_service_start(self) -> None:
        data_dir = Path(tempfile.mkdtemp())
        RuntimeStateStore(data_dir, "job_state.json").save(
            {
                "running": True,
                "reason": "定时",
                "started_at": "2026-11-15T01:10:00+08:00",
                "finished_at": "",
                "results": [],
                "error": "",
                "interrupted": False,
            }
        )
        restored = JobManager(ConfigStore(data_dir)).snapshot()
        self.assertFalse(restored["running"])
        self.assertTrue(restored["interrupted"])
        self.assertIn("重启", restored["error"])

    def test_scheduler_daily_key_survives_restart(self) -> None:
        data_dir = Path(tempfile.mkdtemp())
        first = Scheduler(ConfigStore(data_dir), JobManager(ConfigStore(data_dir)))
        first._claim_key("2026-11-15 01:10", "2026-11-15T01:10:00+08:00")
        restored = Scheduler(ConfigStore(data_dir), JobManager(ConfigStore(data_dir)))
        self.assertEqual(restored._last_key, "2026-11-15 01:10")


if __name__ == "__main__":
    unittest.main()
