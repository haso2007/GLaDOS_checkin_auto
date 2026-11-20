"""No-network tests for scheduled auto-retry of failed accounts."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_store import ConfigStore
from glados import CheckinStatus
from web_service import BrowserGate, JobManager


def _account(account_id: str) -> dict:
    return {
        "id": account_id,
        "enabled": True,
        "name": account_id,
        "email": "",
        "origin": "glados.cloud",
        "cookie": f"gld:sess={account_id}",
        "user_agent": "",
        "proxy": "",
    }


def _entry(account_id: str, code: CheckinStatus) -> SimpleNamespace:
    return SimpleNamespace(
        cookie=f"gld:sess={account_id}",
        label=account_id,
        result=SimpleNamespace(
            code=code,
            domain="glados.cloud",
            points=2 if code is CheckinStatus.SUCCESS else 0,
            points_total=100,
            days=10,
            exchange="",
            note="",
        ),
    )


class _ScriptedRunner:
    """Canned BrowserCheckinRunner: per-account scripted outcomes per attempt."""

    def __init__(self, runtime, proxies=None, account_meta=None, headless=True, cancelled=None):
        self.runtime: dict = {}
        self._account_ids = [meta.account_id for meta in (account_meta or {}).values()]
        self._script = _ScriptedRunner.script
        self._calls = _ScriptedRunner.calls

    def run(self) -> SimpleNamespace:
        self._calls.append(list(self._account_ids))
        entries = []
        for account_id in self._account_ids:
            outcomes = self._script.get(account_id) or [CheckinStatus.SUCCESS]
            code = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
            entries.append(_entry(account_id, code))
        return SimpleNamespace(title="test", render=lambda: "test", entries=entries)


class AutoRetryTests(unittest.TestCase):
    def _jobs(self, config: dict) -> JobManager:
        store = ConfigStore(Path(tempfile.mkdtemp()))
        store.save(config)
        return JobManager(store, BrowserGate())

    def _run_with_script(self, jobs: JobManager, reason: str, script: dict) -> list:
        _ScriptedRunner.script = {key: list(value) for key, value in script.items()}
        _ScriptedRunner.calls = []
        with patch("web_service.BrowserCheckinRunner", _ScriptedRunner):
            jobs._run(reason, None)
        return _ScriptedRunner.calls

    def test_scheduled_run_retries_failures_until_success(self) -> None:
        jobs = self._jobs(
            {
                "accounts": [_account("a"), _account("b")],
                "auto_retry": True,
                "auto_retry_max": 2,
                "auto_retry_delay_seconds": 0,
            }
        )
        calls = self._run_with_script(
            jobs,
            "定时",
            {"a": [CheckinStatus.FAILURE, CheckinStatus.SUCCESS], "b": [CheckinStatus.SUCCESS]},
        )
        self.assertEqual(calls, [["a", "b"], ["a"]])
        by_id = {item["account_id"]: item for item in jobs.snapshot()["results"]}
        self.assertEqual(set(by_id), {"a", "b"})
        self.assertEqual(by_id["a"]["status"], "success")
        self.assertEqual(by_id["b"]["status"], "success")

    def test_scheduled_run_stops_after_max_retries(self) -> None:
        jobs = self._jobs(
            {
                "accounts": [_account("a")],
                "auto_retry": True,
                "auto_retry_max": 2,
                "auto_retry_delay_seconds": 0,
            }
        )
        calls = self._run_with_script(jobs, "定时", {"a": [CheckinStatus.FAILURE]})
        self.assertEqual(calls, [["a"], ["a"], ["a"]])
        self.assertEqual(jobs.snapshot()["results"][0]["status"], "failure")

    def test_manual_run_does_not_auto_retry(self) -> None:
        jobs = self._jobs(
            {
                "accounts": [_account("a")],
                "auto_retry": True,
                "auto_retry_max": 2,
                "auto_retry_delay_seconds": 0,
            }
        )
        calls = self._run_with_script(jobs, "手动", {"a": [CheckinStatus.FAILURE]})
        self.assertEqual(calls, [["a"]])

    def test_auto_retry_off_skips_retry(self) -> None:
        jobs = self._jobs(
            {
                "accounts": [_account("a")],
                "auto_retry": False,
                "auto_retry_max": 2,
                "auto_retry_delay_seconds": 0,
            }
        )
        calls = self._run_with_script(jobs, "定时", {"a": [CheckinStatus.FAILURE]})
        self.assertEqual(calls, [["a"]])

    def test_config_defaults_and_clamps(self) -> None:
        store = ConfigStore(Path(tempfile.mkdtemp()))
        normalized = store._normalize({})
        self.assertTrue(normalized["auto_retry"])
        self.assertEqual(normalized["auto_retry_max"], 2)
        self.assertEqual(normalized["auto_retry_delay_seconds"], 120)
        clamped = store._normalize({"auto_retry_max": 9, "auto_retry_delay_seconds": 9999})
        self.assertEqual(clamped["auto_retry_max"], 5)
        self.assertEqual(clamped["auto_retry_delay_seconds"], 600)


if __name__ == "__main__":
    unittest.main()
