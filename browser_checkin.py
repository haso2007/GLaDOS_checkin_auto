"""Run the existing GLaDOS protocol through isolated browser environments."""

from __future__ import annotations

import json
import logging
import os
import random
import time
import urllib.request
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
from turnstile import VerificationError, has_turnstile_widget, read_turnstile_token, wait_for_turnstile


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountRunMeta:
    """Configuration kept outside CookieEntry so the legacy parser remains compatible."""

    account_id: str
    proxy: str = ""


class BrowserAPI:
    """A GLaDOS API facade whose requests always originate from the browser page."""

    def __init__(self, environment: BrowserEnvironment, domain: str, entry: CookieEntry, verbose: bool = False):
        self.environment = environment
        self.domain = domain
        self.entry = entry
        self.verbose = verbose

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
        Clicking the real `.ckb` control preserves all current fields while the
        response listener still gives the service a structured result.
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
            # The page renders Turnstile after the click. A code=5 response is
            # the site's first rejection while the widget is still solving, so
            # wait for the widget and let the page submit again.
            self.log(LogEmoji.CHECKIN, "点击网页签到按钮，等待 Cloudflare Turnstile")
            button.click(timeout=10_000)
            deadline = time.monotonic() + 110
            attempts = 0
            while time.monotonic() < deadline:
                if responses and accepted(responses[-1]):
                    return parse_checkin(responses[-1], self.domain, self.log)
                token = read_turnstile_token(page)
                if token:
                    self.log(LogEmoji.INFO, f"Cloudflare Turnstile 已通过（token 长度 {len(token)}），重新提交签到", True)
                    responses.clear()
                    button.click(timeout=10_000)
                    continue
                if attempts < 3 and (has_turnstile_widget(page) or responses):
                    attempts += 1
                    self.log(LogEmoji.INFO, f"第 {attempts}/3 次等待 Cloudflare Turnstile", True)
                    try:
                        wait_for_turnstile(
                            page,
                            timeout=min(40, deadline - time.monotonic()),
                            log=lambda message: self.log(LogEmoji.INFO, message, True),
                        )
                    except VerificationError:
                        if not read_turnstile_token(page) and attempts >= 3:
                            raise
                    if read_turnstile_token(page):
                        continue
                    responses.clear()
                    self.log(LogEmoji.WARNING, "验证未签发 token，重新点击签到按钮", True)
                    button.click(timeout=10_000)
                    continue
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
    """Sequentially run one disposable Camoufox environment per configured account."""

    def __init__(self, config: Config, proxies: Iterable[str] = (), account_meta: dict[str, AccountRunMeta] | None = None, headless: bool = False, local_only: bool = False, local_use_account_proxy: bool = False):
        self.config = config
        self.headless = bool(headless)
        self.local_only = bool(local_only)
        self.local_use_account_proxy = bool(local_use_account_proxy)
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
        resolved_domain = entry.origin or "glados.cloud"
        if self.local_only:
            proxy = (proxies[0] if proxies else "") if self.local_use_account_proxy else ""
            entry.log(resolved_domain, LogEmoji.START, f"直接使用本机真实浏览器（{'沿用账号代理：' + proxy_label(proxy) if proxy else '使用本机系统网络'}）", True)
            if self._run_local_browser(entry, meta, proxy, resolved_domain):
                return
            last_error = "本机浏览器未完成签到"
            entry.result = CheckinResult.failure(resolved_domain)
            entry.result.note = last_error
            entry._log_result()
            return
        for attempt, proxy in enumerate(proxies, 1):
            try:
                entry.log(entry.origin or UNPROBED, LogEmoji.START, f"建立独立浏览器环境（第 {attempt}/{len(proxies)} 条代理：{proxy_label(proxy)}）", True)
                with BrowserEnvironment(
                    entry.cookie,
                    proxy=proxy,
                    user_agent=entry.user_agent,
                    headless=self.headless,
                    log=lambda message: entry.log(entry.origin or UNPROBED, LogEmoji.INFO, message, True),
                ) as environment:
                    self.runtime[meta.account_id] = {"proxy": proxy_label(proxy), "exit_ip": environment.exit_ip}
                    domain, days = self._resolve_domain(entry, environment, budget)
                    resolved_domain = domain or resolved_domain
                    if domain is None:
                        entry.result = CheckinResult.failure(entry.origin or UNPROBED)
                        entry.result.note = "未找到有效登录域名"
                    elif budget.expired():
                        entry.result = CheckinResult(domain=domain, days=days)
                        entry.result.note = "运行超时，已跳过签到"
                    else:
                        api = BrowserAPI(environment, domain, entry, self.config.verbose)
                        entry.result = entry._sign_in(api, domain, days, self.config)
                    entry._log_result()
                    return
            except (BrowserEnvironmentError, VerificationError) as exc:
                last_error = str(exc)
                entry.log(entry.origin or UNPROBED, LogEmoji.WARNING, f"浏览器环境失败：{exc}", True)
                if self._run_local_browser(entry, meta, proxy, resolved_domain):
                    return
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.exception("账号 %s 在浏览器签到时出现未预期异常", entry.label)
                entry.log(entry.origin or UNPROBED, LogEmoji.ERROR, f"浏览器签到异常：{exc}", True)

        entry.result = CheckinResult.failure(entry.origin or UNPROBED)
        entry.result.note = f"所有代理均失败：{last_error or '未知错误'}"
        entry._log_result()

    def _run_local_browser(self, entry: CookieEntry, meta: AccountRunMeta, proxy: str, domain: str) -> bool:
        """Ask the Windows machine to finish check-in in its real browser."""
        endpoint = os.environ.get("GLADOS_LOCAL_BROWSER_URL", "").strip().rstrip("/")
        target = domain if domain and domain != UNPROBED else entry.origin
        if not endpoint or not target:
            return False
        entry.log(target, LogEmoji.INFO, "直接调用本机真实浏览器" if self.local_only else "容器浏览器未通过，改用本机真实浏览器", True)
        request = urllib.request.Request(
            f"{endpoint}/checkin",
            data=json.dumps({"cookie": entry.cookie, "domain": target, "proxy": proxy}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            entry.log(target, LogEmoji.WARNING, f"本机浏览器不可用：{exc}", True)
            return False
        if not isinstance(payload, dict) or not isinstance(payload.get("payload"), dict):
            entry.log(target, LogEmoji.WARNING, f"本机浏览器失败：{payload.get('error', '无签到结果') if isinstance(payload, dict) else '响应异常'}", True)
            return False
        browser = payload.get("browser") or "本机浏览器"
        self.runtime[meta.account_id] = {"proxy": f"{proxy_label(proxy)} · {browser}", "exit_ip": self.runtime.get(meta.account_id, {}).get("exit_ip", "")}
        entry.result = parse_checkin(payload["payload"], target, lambda emoji, message, force=False: entry.log(target, emoji, message, True))
        entry._log_result()
        return True

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
