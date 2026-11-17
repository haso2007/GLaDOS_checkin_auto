"""Run the existing GLaDOS protocol through isolated browser environments."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from browser_env import BrowserEnvironment, BrowserEnvironmentError, BrowserResponse, proxy_label
from glados import (
    APIEndpoint,
    CheckinResult,
    CheckinStatus,
    Config,
    CookieEntry,
    LogEmoji,
    Report,
    RunBudget,
    UNPROBED,
    left_days,
    parse_checkin,
    to_int,
)
from turnstile import VerificationError, wait_for_turnstile


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountRunMeta:
    """Configuration kept outside CookieEntry so the legacy parser remains compatible."""

    account_id: str
    proxy: str = ""
    state_dir: str = ""


class BrowserAPI:
    """A GLaDOS API facade whose requests always originate from the browser page."""

    def __init__(self, environment: BrowserEnvironment, domain: str, entry: CookieEntry, verbose: bool = False, security_method: int = 0):
        self.environment = environment
        self.domain = domain
        self.entry = entry
        self.verbose = verbose
        self.security_method = security_method

    def log(self, emoji: str, message: str, force: bool = False) -> None:
        self.entry.log(self.domain, emoji, message, self.verbose or force)

    def _json(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        json_body: bool = False,
    ) -> Optional[dict]:
        response: BrowserResponse = self.environment.request(self.domain, method, path, data, json_body)
        if not response.ok:
            detail = "HTML 响应，已省略" if "html" in response.content_type.lower() or response.text.lstrip().startswith("<") else response.text[:300]
            self.log(LogEmoji.WARNING, f"向 {path} 请求失败，状态码 {response.status}。响应: {detail}", True)
            return None
        if not isinstance(response.body, dict):
            self.log(LogEmoji.ERROR, f"{path} 响应解析失败（非 JSON 对象）", True)
            return None
        return response.body

    def fetch_status(self) -> Optional[dict]:
        return self._json("GET", APIEndpoint.STATUS)

    def checkin(self) -> CheckinResult:
        """Click GLaDOS's own UI so its current client protocol stays intact.

        The current frontend gets a nonce-bound, action=checkin Turnstile token
        and submits it as `turnstile` alongside nonce/proof. Reimplementing
        that private client state lost the required token and returned code 5.
        Login in this same browser first. The Security widget is the 300x68 box
        immediately before Check-in. Click that box once. When its iframe is gone
        and cf-turnstile-response holds a token, click Check-in. Another click in
        the same browser does not recover a rejected challenge.
        """
        self.environment.open_domain(self.domain)
        page = self.environment.page
        if page is None:
            raise BrowserEnvironmentError("浏览器页面未就绪，无法执行签到")
        responses: list[dict[str, Any]] = []

        def capture(response: Any) -> None:
            try:
                request = response.request
                if request.method == "POST" and "/api/user/checkin" in str(response.url):
                    payload = response.json()
                    if isinstance(payload, dict):
                        responses.append(payload)
            except Exception:
                pass

        def accepted(payload: dict[str, Any]) -> bool:
            message = str(payload.get("message") or "")
            return payload.get("code") in (0, 1) or "cloudflare verification failed" not in message.lower()

        page.on("response", capture)
        try:
            button = page.locator("button.ckb").first
            try:
                button.wait_for(state="visible", timeout=20_000)
            except Exception as exc:
                raise BrowserEnvironmentError("未找到 GLaDOS 签到按钮，页面结构可能已更新或尚未完成加载") from exc
            def click_checkin() -> None:
                try:
                    button.click(timeout=15_000)
                except Exception as exc:
                    self.log(LogEmoji.WARNING, f"签到按钮常规点击受阻，改用页面按钮事件重试：{exc}", True)
                    button.click(timeout=15_000, force=True)

            self.log(LogEmoji.INFO, "在当前桌面浏览器里点击画面上的 Security，并轮询等待通过", True)
            token = wait_for_turnstile(
                page,
                timeout=40,
                log=lambda message: self.log(LogEmoji.INFO, message, True),
            )
            self.log(LogEmoji.CHECKIN, f"Security 已通过（token 长度 {len(token)}），点击 Check-in")
            click_checkin()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if responses and accepted(responses[-1]):
                    return parse_checkin(responses[-1], self.domain, self.log)
                page.wait_for_timeout(500)
            if responses and accepted(responses[-1]):
                return parse_checkin(responses[-1], self.domain, self.log)
            if responses:
                raise VerificationError(str(responses[-1].get("message") or "Cloudflare verification failed"))
            raise VerificationError("签到页面未在 110 秒内返回结果")
        finally:
            try:
                page.remove_listener("response", capture)
            except Exception:
                pass

    def get_points(self) -> Optional[int]:
        data = self._json("GET", APIEndpoint.POINTS)
        if data is None:
            return None
        points = to_int(data.get("points"))
        if points is None:
            self.log(LogEmoji.ERROR, f"积分字段无法解析: {data.get('points')!r}", True)
            return None
        self.log(LogEmoji.SUCCESS, f"{{ code : {data.get('code', -2)}, points : {points} 积分}}")
        return points

    def exchange(self, plan: str, label: str = "") -> str:
        endpoints = [
            APIEndpoint.EXCHANGE,
            APIEndpoint.EXCHANGE_FALLBACK,
            "/api/user/points/convert",
            "/api/user/points/redeem",
        ]
        data: Optional[dict] = None
        for endpoint in endpoints:
            data = self._json("POST", endpoint, {"planType": plan}, json_body=True)
            if data is None:
                data = self._json("POST", endpoint, {"planType": plan}, json_body=False)
            if data is not None:
                break

        tag = label or plan
        if data is None:
            self.log(LogEmoji.WARNING, f"兑换 {tag} 未完成（接口无有效响应），请到站点确认", True)
            return f"exchange {tag}--未完成"
        message = data.get("message") or data.get("msg") or str(data)
        if data.get("code") == 0:
            self.log(LogEmoji.SUCCESS, f"exchange {tag}--{message}")
            return f"exchange {tag}--{message}"
        self.log(LogEmoji.FAIL, f"exchange {tag}--{message}", True)
        return f"exchange {tag}--{message}"


class BrowserCheckinRunner:
    """Sequentially run one disposable native Chromium environment per account."""

    def __init__(self, config: Config, proxies: Iterable[str] = (), account_meta: dict[str, AccountRunMeta] | None = None, headless: bool = True):
        self.config = config
        self.headless = bool(headless)
        self.proxies = list(proxies)
        self.account_meta = account_meta or {}
        self.runtime: dict[str, dict[str, str]] = {}

    def run(self, account_ids: set[str] | None = None, budget: RunBudget | None = None) -> Report:
        entries = list(self.config.accounts)
        budget = budget or RunBudget()
        selected: list[tuple[int, CookieEntry, AccountRunMeta]] = []
        for index, entry in enumerate(entries):
            account_id = str(index + 1)
            meta = self.account_meta.get(account_id, AccountRunMeta(account_id))
            if account_ids is None or meta.account_id in account_ids:
                selected.append((index, entry, meta))

        for position, (index, entry, meta) in enumerate(selected):
            if position and not self._wait_between_accounts(entry, budget):
                entry.result = CheckinResult.failure(entry.origin or UNPROBED)
                entry.result.note = "运行超时，已跳过"
                continue
            if budget.expired():
                entry.result = CheckinResult.failure(entry.origin or UNPROBED)
                entry.result.note = "运行超时，已跳过"
                continue
            self._run_account(entry, meta, index, budget)
        return Report(entries)

    @staticmethod
    def _wait_between_accounts(entry: CookieEntry, budget: RunBudget) -> bool:
        """Use a small random gap between accounts without exceeding the job budget."""
        seconds = random.randint(10, 20)
        entry.log(entry.origin or UNPROBED, LogEmoji.INFO, f"账号间隔等待 {seconds} 秒后继续", True)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if budget.expired():
                return False
            time.sleep(min(0.5, deadline - time.monotonic()))
        return not budget.expired()

    def _candidate_proxies(self, meta: AccountRunMeta, index: int) -> list[str]:
        if meta.proxy:
            return [meta.proxy]
        if not self.proxies:
            return [""]
        ordered = self.proxies[index % len(self.proxies) :] + self.proxies[: index % len(self.proxies)]
        return ordered

    def _run_account(self, entry: CookieEntry, meta: AccountRunMeta, index: int, budget: RunBudget) -> None:
        proxies = self._candidate_proxies(meta, index)
        last_error = ""
        for attempt, proxy in enumerate(proxies, 1):
            try:
                entry.log(
                    entry.origin or UNPROBED,
                    LogEmoji.START,
                    f"使用该账号的独立浏览器环境，代理 {attempt}/{len(proxies)}：{proxy_label(proxy)}",
                    True,
                )
                self._run_one_environment(entry, meta, proxy, budget)
                return
            except (BrowserEnvironmentError, VerificationError) as exc:
                last_error = str(exc)
                entry.log(entry.origin or UNPROBED, LogEmoji.WARNING, f"本浏览器环境失败，关闭后换下一个代理：{exc}", True)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.exception("账号 %s 在浏览器签到时出现未预期异常", entry.label)
                entry.log(entry.origin or UNPROBED, LogEmoji.ERROR, f"浏览器签到异常：{exc}", True)

        entry.result = CheckinResult.failure(entry.origin or UNPROBED)
        entry.result.note = f"所有浏览器环境均失败：{last_error or '未知错误'}"
        entry._log_result()

    def _run_one_environment(self, entry: CookieEntry, meta: AccountRunMeta, proxy: str, budget: RunBudget) -> None:
        with BrowserEnvironment(
            entry.cookie,
            proxy=proxy,
            user_agent=entry.user_agent,
            headless=self.headless,
            log=lambda message: entry.log(entry.origin or UNPROBED, LogEmoji.INFO, message, True),
            state_dir=meta.state_dir or None,
        ) as environment:
            self.runtime[meta.account_id] = {"proxy": proxy_label(proxy), "exit_ip": environment.exit_ip}
            domain, days = self._resolve_domain(entry, environment, budget)
            if domain is None:
                entry.result = CheckinResult.failure(entry.origin or UNPROBED)
                entry.result.note = "未找到有效登录域名"
            elif budget.expired():
                entry.result = CheckinResult(domain=domain, days=days)
                entry.result.note = "运行超时，已跳过签到"
            else:
                api = BrowserAPI(environment, domain, entry, self.config.verbose)
                entry.result = entry._sign_in(api, domain, days, self.config)
                if entry.result is not None and entry.result.code in (0, 1):
                    try:
                        environment.save_state()
                    except Exception as exc:  # noqa: BLE001
                        entry.log(domain, LogEmoji.WARNING, f"签到成功，但账号缓存未写回：{exc}", True)
            entry._log_result()

    def _resolve_domain(
        self,
        entry: CookieEntry,
        environment: BrowserEnvironment,
        budget: RunBudget,
    ) -> tuple[Optional[str], Optional[int]]:
        rejected: list[str] = []
        network: list[str] = []
        for domain in entry._candidates(self.config):
            if budget.expired():
                return None, None
            api = BrowserAPI(environment, domain, entry, self.config.verbose)
            try:
                data = api.fetch_status()
            except (BrowserEnvironmentError, VerificationError):
                raise
            except Exception as exc:  # noqa: BLE001
                network.append(domain)
                entry.log(domain, LogEmoji.WARNING, f"状态探测失败：{exc}", True)
                continue
            if isinstance(data, dict) and data.get("code") == 0 and isinstance(data.get("data"), dict):
                entry.update_name(str(data.get("data", {}).get("email") or data.get("data", {}).get("name") or ""))
                days = left_days(data)
                entry.log(domain, LogEmoji.SUCCESS, f"命中登录域名（剩余 {days if days is not None else '未知'} 天）", True)
                return domain, days
            if data is None:
                network.append(domain)
            else:
                rejected.append(f"{domain}(code={data.get('code', -2)})")

        if rejected:
            entry.log(entry.origin or UNPROBED, LogEmoji.WARNING, f"未命中登录域名：{'、'.join(rejected)}", True)
        if network:
            entry.log(entry.origin or UNPROBED, LogEmoji.WARNING, f"无响应域名：{'、'.join(network)}", True)
        return None, None
