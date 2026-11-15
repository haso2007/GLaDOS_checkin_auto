"""FastAPI console, scheduler, and serialized browser check-in jobs."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from browser_checkin import AccountRunMeta, BrowserCheckinRunner
from browser_env import parse_proxy_lines
from config_store import ConfigStore
from glados import CheckinStatus, Config, CookieEntry
from logging_config import BEIJING_TZ, append_persistent_log, init_logger, log_file_path, query_persistent_logs, read_recent_logs, redact


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "web" / "index.html"


class _PersistentLogHandler(logging.Handler):
    """Keep application logs in the capped history file and the on-screen buffer."""

    def __init__(self, job: "JobManager") -> None:
        super().__init__(level=logging.INFO)
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("uvicorn"):
            return
        message = redact(record.getMessage())
        if record.exc_info:
            message = f"{message}\n{redact(self.formatException(record.exc_info))}"
        self.job.append_log(message, record.levelname)


class JobManager:
    """Runs at most one browser job at a time and exposes its safe status to the UI."""

    def __init__(self, store: ConfigStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._next_log_id = 0
        self._state: dict[str, Any] = {
            "running": False,
            "reason": "",
            "started_at": "",
            "finished_at": "",
            "logs": read_recent_logs(500),
            "results": [],
            "error": "",
        }
        self._next_log_id = max((int(item.get("id") or 0) for item in self._state["logs"]), default=0)

    def snapshot(self, after: int = 0) -> dict[str, Any]:
        with self._lock:
            state = copy.deepcopy(self._state)
        if after > 0:
            state["logs"] = [item for item in state["logs"] if int(item.get("id") or 0) > after]
        return state

    def append_log(self, message: str, level: str = "INFO") -> None:
        stamp = append_persistent_log(message, level)
        with self._lock:
            self._next_log_id += 1
            lines: list[dict[str, Any]] = self._state["logs"]
            lines.append(
                {
                    "id": self._next_log_id,
                    "time": stamp,
                    "level": str(level or "INFO").upper(),
                    "message": redact(str(message)),
                }
            )
            if len(lines) > 500:
                del lines[:-500]

    def start(self, reason: str, account_id: str | None = None, local_only: bool = False) -> bool:
        with self._lock:
            if self._state["running"]:
                return False
            self._state.update(
                {
                    "running": True,
                    "reason": reason,
                    "started_at": datetime.now(BEIJING_TZ).isoformat(),
                    "finished_at": "",
                    "results": [],
                    "error": "",
                }
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(reason, account_id, local_only),
                name="glados-browser-checkin",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self, reason: str, account_id: str | None, local_only: bool = False) -> None:
        try:
            logger.info("开始%s签到%s", reason, f"：指定账号 {account_id}" if account_id else "")
            config_data = self.store.load()
            selected = self._select_accounts(config_data, account_id)
            if not selected:
                raise RuntimeError("没有可执行账号；请添加并启用至少一个包含 gld:sess 或 koa:sess 的 Cookie")

            invalid = [item for item in selected if not CookieEntry.has_session_field(item.get("cookie", ""))]
            for item in invalid:
                logger.warning("账号 %s 的 Cookie 无效，已跳过", item.get("name") or item["id"])
            selected = [item for item in selected if item not in invalid]
            if not selected:
                raise RuntimeError("所选账号的 Cookie 均无效")

            runtime = self._make_runtime_config(config_data, selected)
            account_meta = {
                str(index): AccountRunMeta(account_id=item["id"], proxy=item.get("proxy", ""))
                for index, item in enumerate(selected, 1)
            }
            runner = BrowserCheckinRunner(
                runtime,
                proxies=parse_proxy_lines(config_data.get("proxies_text", "")),
                account_meta=account_meta,
                headless=bool(config_data.get("browser_headless", False)),
                local_only=local_only or bool(config_data.get("browser_local", False)),
                local_use_account_proxy=bool(config_data.get("local_browser_use_account_proxy", False)),
            )
            report = runner.run()
            logger.info("========== 签到总结 ==========")
            logger.info("%s", report.title)
            for line in report.render().splitlines():
                logger.info("%s", line)
            results = self._serialize_results(report.entries, selected, runner.runtime)
            with self._lock:
                self._state["results"] = results
        except Exception as exc:  # noqa: BLE001
            logger.exception("任务失败：%s", exc)
            with self._lock:
                self._state["error"] = str(exc)
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["finished_at"] = datetime.now(BEIJING_TZ).isoformat()

    @staticmethod
    def _select_accounts(config: dict[str, Any], account_id: str | None) -> list[dict[str, Any]]:
        accounts = [item for item in config.get("accounts", []) if isinstance(item, dict)]
        if account_id:
            selected = [item for item in accounts if item.get("id") == account_id]
            if not selected:
                raise RuntimeError("指定账号不存在")
            return selected
        return [item for item in accounts if item.get("enabled", True)]

    @staticmethod
    def _make_runtime_config(config: dict[str, Any], accounts: list[dict[str, Any]]) -> Config:
        payload = [
            {
                "name": item.get("name", ""),
                "origin": item.get("origin", ""),
                "cookie": item.get("cookie", ""),
                "user-agent": item.get("user_agent", ""),
            }
            for item in accounts
        ]
        env = {
            "GLADOS_COOKIES": json.dumps(payload, ensure_ascii=False),
            "AUTO_EXCHANGE": "true" if config.get("auto_exchange", True) else "false",
            "AUTO_EXCHANGE_LEFT_DAYS": str(config.get("auto_exchange_left_days", 1)),
            "GLADOS_VERBOSE": "true" if config.get("verbose", False) else "false",
            "GLADOS_BASE_URLS": str(config.get("base_urls", "")),
        }
        return Config(env)

    @staticmethod
    def _serialize_results(entries: list[Any], selected: list[dict[str, Any]], runtime: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
        by_cookie = {item.get("cookie"): item for item in selected}
        output = []
        for entry in entries:
            source = by_cookie.get(entry.cookie, {})
            result = entry.result
            status = {
                CheckinStatus.SUCCESS: "success",
                CheckinStatus.REPEAT: "repeat",
                CheckinStatus.FAILURE: "failure",
            }.get(result.code, "failure")
            metadata = runtime.get(str(source.get("id") or ""), {})
            output.append(
                {
                    "account_id": source.get("id", ""),
                    "name": entry.label,
                    "status": status,
                    "domain": result.domain,
                    "points": result.points,
                    "points_total": result.points_total,
                    "days": result.days,
                    "exchange": result.exchange,
                    "note": result.note,
                    "proxy": metadata.get("proxy", ""),
                    "exit_ip": metadata.get("exit_ip", ""),
                }
            )
        return output


class Scheduler:
    """Small in-process daily scheduler; persistence is not needed beyond each date key."""

    def __init__(self, store: ConfigStore, jobs: JobManager) -> None:
        self.store = store
        self.jobs = jobs
        self._stop = threading.Event()
        self._last_key = ""
        self._thread = threading.Thread(target=self._loop, name="glados-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(5):
            try:
                config = self.store.load()
                schedule = config.get("scheduler", {})
                if not schedule.get("enabled"):
                    continue
                now = datetime.now(ZoneInfo("Asia/Shanghai"))
                if now.strftime("%H:%M") != schedule.get("time"):
                    continue
                key = now.strftime("%Y-%m-%d %H:%M")
                if key == self._last_key:
                    continue
                self._last_key = key
                if self.jobs.start("定时"):
                    logger.info("已启动 %s 自动签到", key)
                else:
                    logger.warning("%s 定时签到跳过：已有任务执行中", key)
            except Exception:  # noqa: BLE001
                logger.exception("定时签到调度器异常")


def create_app() -> FastAPI:
    init_logger()
    store = ConfigStore()
    jobs = JobManager(store)
    logging.getLogger().addHandler(_PersistentLogHandler(jobs))
    scheduler = Scheduler(store, jobs)
    app = FastAPI(title="GLaDOS Browser Check-in")
    app.state.store = store
    app.state.jobs = jobs
    app.state.scheduler = scheduler

    @app.on_event("startup")
    def startup() -> None:
        scheduler.start()

    @app.on_event("shutdown")
    def shutdown() -> None:
        scheduler.stop()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(INDEX)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": "browser-cf-1", "running": jobs.snapshot()["running"]}

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return {"config": store.load()}

    @app.put("/api/config")
    def put_config(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return {"config": store.save(payload)}

    @app.get("/api/job")
    def get_job(after: int = 0) -> dict[str, Any]:
        return jobs.snapshot(after)

    @app.get("/api/logs/history")
    def log_history(keyword: str = "", limit: int = 1000) -> dict[str, Any]:
        return query_persistent_logs(keyword, limit)

    @app.get("/api/logs/download")
    def download_logs() -> FileResponse:
        path = log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        return FileResponse(path, filename="glados.log", media_type="text/plain; charset=utf-8")

    @app.post("/api/run")
    def run_all() -> dict[str, Any]:
        if not jobs.start("手动"):
            raise HTTPException(status_code=409, detail="已有签到任务正在执行")
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/run")
    def run_one(account_id: str) -> dict[str, Any]:
        if not jobs.start("单账号手动", account_id=account_id):
            raise HTTPException(status_code=409, detail="已有签到任务正在执行")
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/run-local")
    def run_one_local(account_id: str) -> dict[str, Any]:
        if not jobs.start("单账号本机浏览器", account_id=account_id, local_only=True):
            raise HTTPException(status_code=409, detail="已有签到任务正在执行")
        return {"ok": True}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("WEB_HOST", "127.0.0.1"), port=int(os.environ.get("WEB_PORT", "8791")))
