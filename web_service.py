"""FastAPI console, scheduler, and serialized browser check-in jobs."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from browser_checkin import AccountRunMeta, BrowserCheckinRunner
from browser_env import BrowserEnvironmentError, normalize_proxy, parse_proxy_lines
from browser_state import cache_ready, ensure_account_states, state_dir
from browser_login import (
    CODE_WAIT_SECONDS,
    CodeRejected,
    LoginBrowser,
    LoginCancelled,
    LoginError,
    start_login,
)
from config_store import ConfigStore
from glados import CheckinStatus, Config, CookieEntry
from logging_config import BEIJING_TZ, append_persistent_log, init_logger, log_file_path, query_persistent_logs, read_recent_logs, redact


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "web" / "index.html"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HOST = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_LOGIN_ACTIVE = {"opening", "awaiting_code", "submitting"}


class BrowserGate:
    """One Chromium task at a time, shared by check-in and re-login."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.holder = ""

    def try_acquire(self, holder: str) -> bool:
        with self._lock:
            if self.holder:
                return False
            self.holder = holder
            return True

    def release(self, holder: str) -> None:
        with self._lock:
            if self.holder == holder:
                self.holder = ""


def _normalize_email(value: str) -> str:
    email = str(value or "").strip()
    if not _EMAIL.fullmatch(email):
        raise ValueError("请填写有效邮箱")
    return email


def _normalize_origin(value: str) -> str:
    text = re.sub(r"^https?://", "", str(value or "").strip(), flags=re.IGNORECASE)
    host = text.split("/")[0].strip().lower()
    if not _HOST.fullmatch(host):
        raise ValueError("请填写登录域名")
    return host


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


class RuntimeStateStore:
    """Atomically persist small non-secret operational snapshots beside config.json."""

    def __init__(self, data_dir: Path, filename: str) -> None:
        self.path = data_dir / filename
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
        return value if isinstance(value, dict) else {}

    def save(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
            finally:
                temporary.unlink(missing_ok=True)


class JobManager:
    """Runs at most one browser job at a time and exposes its safe status to the UI."""

    def __init__(self, store: ConfigStore, gate: BrowserGate | None = None) -> None:
        self.store = store
        self.gate = gate or BrowserGate()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._next_log_id = 0
        self._state_store = RuntimeStateStore(store.data_dir, "job_state.json")
        self._state: dict[str, Any] = {
            "running": False,
            "reason": "",
            "started_at": "",
            "finished_at": "",
            "logs": read_recent_logs(500),
            "results": [],
            "error": "",
            "interrupted": False,
        }
        self._restore_state()
        self._next_log_id = max((int(item.get("id") or 0) for item in self._state["logs"]), default=0)

    def _restore_state(self) -> None:
        saved = self._state_store.load()
        if not saved:
            return
        self._state.update(
            {
                "running": bool(saved.get("running", False)),
                "reason": str(saved.get("reason") or ""),
                "started_at": str(saved.get("started_at") or ""),
                "finished_at": str(saved.get("finished_at") or ""),
                "results": [item for item in saved.get("results", []) if isinstance(item, dict)],
                "error": str(saved.get("error") or ""),
                "interrupted": bool(saved.get("interrupted", False)),
            }
        )
        if self._state["running"]:
            self._state.update(
                {
                    "running": False,
                    "finished_at": datetime.now(BEIJING_TZ).isoformat(),
                    "error": "服务在任务执行期间重启；上一轮结果可能不完整，请按需重试失败账号。",
                    "interrupted": True,
                }
            )
            self._persist_locked()

    def _persist_locked(self) -> None:
        self._state_store.save(
            {
                "version": 1,
                "running": self._state["running"],
                "reason": self._state["reason"],
                "started_at": self._state["started_at"],
                "finished_at": self._state["finished_at"],
                "results": self._state["results"],
                "error": self._state["error"],
                "interrupted": self._state["interrupted"],
            }
        )

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

    def start(
        self,
        reason: str,
        account_id: str | None = None,
        account_ids: set[str] | None = None,
        on_accept: Callable[[], None] | None = None,
    ) -> bool:
        """Start a job, optionally atomically claiming an external scheduler slot."""
        selected_ids = set(account_ids) if account_ids is not None else None
        with self._lock:
            if self._state["running"]:
                return False
            if not self.gate.try_acquire("checkin"):
                return False
            try:
                if on_accept is not None:
                    on_accept()
                self._state.update(
                    {
                        "running": True,
                        "reason": reason,
                        "started_at": datetime.now(BEIJING_TZ).isoformat(),
                        "finished_at": "",
                        "results": [],
                        "error": "",
                        "interrupted": False,
                    }
                )
                self._persist_locked()
                self._thread = threading.Thread(
                    target=self._run,
                    args=(reason, account_id, selected_ids),
                    name="glados-browser-checkin",
                    daemon=True,
                )
                self._thread.start()
            except Exception:
                self.gate.release("checkin")
                if self._state["running"]:
                    self._state.update(
                        {
                            "running": False,
                            "finished_at": datetime.now(BEIJING_TZ).isoformat(),
                            "error": "无法启动签到后台任务",
                        }
                    )
                    self._persist_locked()
                raise
            return True

    def retry_failed(self) -> tuple[bool, int]:
        """Retry only the failures produced by the most recently completed job."""
        with self._lock:
            if self._state["running"]:
                return False, 0
            failed_ids = {
                str(item.get("account_id") or "")
                for item in self._state.get("results", [])
                if item.get("status") == "failure" and item.get("account_id")
            }
        if not failed_ids:
            return False, 0
        return self.start("重试失败账号", account_ids=failed_ids), len(failed_ids)

    def _run(
        self,
        reason: str,
        account_id: str | None,
        account_ids: set[str] | None = None,
    ) -> None:
        try:
            selection_note = f"：指定账号 {account_id}" if account_id else (f"：共 {len(account_ids)} 个失败账号" if account_ids is not None else "")
            logger.info("开始%s签到%s", reason, selection_note)
            config_data = self.store.load()
            selected = self._select_accounts(config_data, account_id, account_ids)
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
                str(index): AccountRunMeta(
                    account_id=item["id"],
                    proxy=item.get("proxy", ""),
                    state_dir=str(state_dir(self.store.data_dir, item["id"])),
                )
                for index, item in enumerate(selected, 1)
            }
            runner = BrowserCheckinRunner(
                runtime,
                proxies=parse_proxy_lines(config_data.get("proxies_text", "")),
                account_meta=account_meta,
                headless=config_data.get("browser_mode", "headless") == "headless",
            )
            report = runner.run()
            logger.info("========== 签到总结 ==========")
            logger.info("%s", report.title)
            for line in report.render().splitlines():
                logger.info("%s", line)
            results = self._serialize_results(report.entries, selected, runner.runtime)
            with self._lock:
                self._state["results"] = results
                self._persist_locked()
        except Exception as exc:  # noqa: BLE001
            logger.exception("任务失败：%s", exc)
            with self._lock:
                self._state["error"] = str(exc)
                self._persist_locked()
        finally:
            self.gate.release("checkin")
            with self._lock:
                self._state["running"] = False
                self._state["finished_at"] = datetime.now(BEIJING_TZ).isoformat()
                self._persist_locked()

    @staticmethod
    def _select_accounts(
        config: dict[str, Any],
        account_id: str | None,
        account_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        accounts = [item for item in config.get("accounts", []) if isinstance(item, dict)]
        if account_id:
            selected = [item for item in accounts if item.get("id") == account_id]
            if not selected:
                raise RuntimeError("指定账号不存在")
            return selected
        if account_ids is not None:
            selected = [item for item in accounts if item.get("enabled", True) and str(item.get("id") or "") in account_ids]
            if not selected:
                raise RuntimeError("上一轮失败账号已被删除或禁用，暂无可重试账号")
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


class LoginManager:
    """Keeps one native Chromium login open until the operator submits the email code."""

    def __init__(self, store: ConfigStore, gate: BrowserGate) -> None:
        self.store = store
        self.gate = gate
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._browser: LoginBrowser | None = None
        self._cancel = threading.Event()
        self._code_event = threading.Event()
        self._attempt_event = threading.Event()
        self._pending_code = ""
        self._attempt_error = ""
        self._cookie = ""
        self._state: dict[str, Any] = {
            "state": "idle",
            "account_id": "",
            "email": "",
            "origin": "",
            "error": "",
            "logs": [],
            "revision": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def busy(self) -> bool:
        with self._lock:
            return self._state["state"] in _LOGIN_ACTIVE

    def start(self, account_id: str, email: str, origin: str) -> None:
        normalized_email = _normalize_email(email)
        normalized_origin = _normalize_origin(origin)
        config = self.store.load()
        account = next((item for item in config.get("accounts", []) if item.get("id") == account_id), None)
        if account is None:
            raise LookupError(account_id)
        with self._lock:
            if self._state["state"] in _LOGIN_ACTIVE:
                raise RuntimeError("已有重新登录正在执行")
            if not self.gate.try_acquire("relogin"):
                raise RuntimeError("已有签到或重新登录正在执行")
            self._cancel.clear()
            self._code_event.clear()
            self._attempt_event.clear()
            self._pending_code = ""
            self._attempt_error = ""
            self._cookie = ""
            self._state.update(
                {
                    "state": "opening",
                    "account_id": account_id,
                    "email": normalized_email,
                    "origin": normalized_origin,
                    "error": "",
                    "logs": [],
                }
            )
            self._state["revision"] += 1
            headless = config.get("browser_mode", "headless") == "headless"
            self._thread = threading.Thread(
                target=self._run,
                args=(account, normalized_email, normalized_origin, headless),
                name="glados-relogin",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception:
                self._state["state"] = "failed"
                self._state["error"] = "未能启动重新登录"
                self._state["revision"] += 1
                self.gate.release("relogin")
                raise

    def submit_code(self, code: str) -> dict[str, str]:
        token = str(code or "").strip()
        if not re.fullmatch(r"\d{4,8}", token):
            raise ValueError("验证码应为 4 到 8 位数字")
        with self._lock:
            if self._state["state"] != "awaiting_code":
                raise RuntimeError("当前没有等待验证码的登录")
            self._attempt_event.clear()
            self._attempt_error = ""
            self._pending_code = token
            self._code_event.set()
        if not self._attempt_event.wait(150):
            raise TimeoutError("提交验证码超时")
        with self._lock:
            if self._state["state"] == "awaiting_code":
                raise CodeRejected(self._attempt_error or "验证码未通过，请再试一次")
            if self._state["state"] != "success":
                raise LoginError(self._state["error"] or "重新登录失败")
            cookie = self._cookie
            self._cookie = ""
            return {"cookie": cookie, "origin": self._state["origin"], "email": self._state["email"]}

    def cancel(self) -> None:
        self._cancel.set()
        self._code_event.set()

    def _run(self, account: dict[str, Any], email: str, origin: str, headless: bool) -> None:
        browser: LoginBrowser | None = None
        try:
            proxies = self._proxies(account)
            account_state = state_dir(self.store.data_dir, str(account.get("id") or ""))
            account_state.mkdir(parents=True, exist_ok=True)
            browser = start_login(
                origin,
                email,
                proxies,
                headless,
                self._login_log,
                self._cancel.is_set,
                account_state,
            )
            self._browser = browser
            self._set("awaiting_code")
            deadline = time.monotonic() + CODE_WAIT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LoginError("等待验证码超时")
                if not self._code_event.wait(remaining):
                    raise LoginError("等待验证码超时")
                if self._cancel.is_set():
                    raise LoginCancelled("已取消")
                with self._lock:
                    code = self._pending_code
                    self._pending_code = ""
                    self._code_event.clear()
                if not code:
                    continue
                self._set("submitting")
                try:
                    cookie = browser.submit_code(code)
                except CodeRejected as exc:
                    self._attempt_error = str(exc)
                    self._set("awaiting_code", str(exc))
                    self._attempt_event.set()
                    continue
                self.store.update_account_session(str(account.get("id") or ""), cookie=cookie, origin=origin, email=email)
                browser.environment.save_state()
                self._login_log("已保存该账号的独立浏览器缓存")
                with self._lock:
                    self._cookie = cookie
                self._set("success")
                logger.info("重新登录成功，已更新账号 Cookie")
                self._attempt_event.set()
                return
        except LoginCancelled:
            logger.info("重新登录已取消")
            self._set("failed", "已取消")
        except ValueError as exc:
            logger.warning("重新登录失败：%s", exc)
            self._set("failed", str(exc))
        except (LoginError, BrowserEnvironmentError) as exc:
            logger.warning("重新登录失败：%s", exc)
            self._set("failed", str(exc))
        except Exception:
            logger.exception("重新登录出现未预期异常")
            self._set("failed", "重新登录失败")
        finally:
            if browser is not None:
                browser.close()
            self._browser = None
            self.gate.release("relogin")
            self._attempt_event.set()

    def _proxies(self, account: dict[str, Any]) -> list[str]:
        dedicated = str(account.get("proxy") or "").strip()
        if dedicated:
            return [normalize_proxy(dedicated)]
        config = self.store.load()
        proxies = parse_proxy_lines(config.get("proxies_text", ""))
        return proxies or [""]

    def _login_log(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        logger.info("%s", text)
        with self._lock:
            logs = list(self._state.get("logs") or [])
            logs.append(text)
            self._state["logs"] = logs[-80:]
            self._state["revision"] += 1

    def _set(self, state: str, error: str = "") -> None:
        with self._lock:
            self._state["state"] = state
            self._state["error"] = error
            self._state["revision"] += 1


class Scheduler:
    """Small in-process daily scheduler with a persistent daily-run claim."""

    def __init__(self, store: ConfigStore, jobs: JobManager) -> None:
        self.store = store
        self.jobs = jobs
        self._stop = threading.Event()
        self._state_store = RuntimeStateStore(store.data_dir, "scheduler_state.json")
        self._last_key = str(self._state_store.load().get("last_started_key") or "")
        self._thread = threading.Thread(target=self._loop, name="glados-scheduler", daemon=True)

    def _claim_key(self, key: str, started_at: str) -> None:
        """Persist the accepted scheduler key before its job thread can run."""
        self._state_store.save({"version": 1, "last_started_key": key, "started_at": started_at})
        self._last_key = key

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
                if self.jobs.start("定时", on_accept=lambda: self._claim_key(key, now.isoformat())):
                    logger.info("已启动 %s 自动签到", key)
                else:
                    logger.warning("%s 定时签到跳过：已有任务执行中", key)
            except Exception:  # noqa: BLE001
                logger.exception("定时签到调度器异常")


def create_app() -> FastAPI:
    init_logger()
    store = ConfigStore()
    gate = BrowserGate()
    jobs = JobManager(store, gate)
    logins = LoginManager(store, gate)
    logging.getLogger().addHandler(_PersistentLogHandler(jobs))
    scheduler = Scheduler(store, jobs)
    app = FastAPI(title="GLaDOS Browser Check-in")
    app.state.store = store
    app.state.jobs = jobs
    app.state.logins = logins
    app.state.scheduler = scheduler

    @app.on_event("startup")
    def startup() -> None:
        config = store.load()
        ensure_account_states(store.data_dir, {str(item.get("id") or "") for item in config.get("accounts", []) if item.get("id")})
        scheduler.start()

    @app.on_event("shutdown")
    def shutdown() -> None:
        scheduler.stop()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(INDEX)

    @app.get("/api/health")
    def health(deep: bool = False) -> JSONResponse:
        """Report browser readiness; deep mode launches only a local about:blank page."""
        from browser_env import chromium_executable, smoke_chromium

        try:
            executable = chromium_executable()
            browser: dict[str, Any] = {"ready": True, "executable": executable}
            if deep:
                browser["smoke"] = smoke_chromium()
        except Exception as exc:  # noqa: BLE001
            browser = {"ready": False, "error": str(exc)}
        ready = bool(browser.get("ready"))
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "ok": ready,
                "version": "native-chromium-1",
                "running": jobs.snapshot()["running"] or logins.busy(),
                "browser": browser,
            },
        )

    def present_config() -> dict[str, Any]:
        config = store.load()
        for account in config.get("accounts", []):
            account["browser_cache"] = cache_ready(store.data_dir, str(account.get("id") or ""))
        return config

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return {"config": present_config()}

    @app.put("/api/config")
    def put_config(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        saved = store.save(payload)
        ensure_account_states(store.data_dir, {str(item.get("id") or "") for item in saved.get("accounts", []) if item.get("id")})
        return {"config": present_config()}

    @app.get("/api/job")
    def get_job(after: int = 0) -> dict[str, Any]:
        snapshot = jobs.snapshot(after)
        if logins.busy():
            snapshot["running"] = True
            snapshot["reason"] = "重新登录"
        return snapshot

    @app.get("/api/relogin")
    def get_relogin() -> dict[str, Any]:
        return logins.snapshot()

    @app.post("/api/accounts/{account_id}/relogin")
    def start_relogin(account_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            logins.start(account_id, str(payload.get("email") or ""), str(payload.get("origin") or ""))
        except LookupError:
            raise HTTPException(status_code=404, detail="指定账号不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "relogin": logins.snapshot()}

    @app.post("/api/relogin/code")
    def submit_relogin_code(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            result = logins.submit_code(str(payload.get("code") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CodeRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=408, detail=str(exc)) from exc
        except LoginError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/api/relogin/cancel")
    def cancel_relogin() -> dict[str, Any]:
        logins.cancel()
        return {"ok": True}

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
            raise HTTPException(status_code=409, detail="已有签到或重新登录正在执行")
        return {"ok": True}

    @app.post("/api/run-failed")
    def run_failed() -> dict[str, Any]:
        started, count = jobs.retry_failed()
        if not started:
            raise HTTPException(status_code=400, detail="上一轮没有可重试的失败账号，或已有任务正在执行")
        return {"ok": True, "count": count}

    @app.post("/api/accounts/{account_id}/run")
    def run_one(account_id: str) -> dict[str, Any]:
        if not jobs.start("单账号手动", account_id=account_id):
            raise HTTPException(status_code=409, detail="已有签到或重新登录正在执行")
        return {"ok": True}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("WEB_HOST", "127.0.0.1"), port=int(os.environ.get("WEB_PORT", "3000")))
