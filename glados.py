import functools
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from logging_config import init_logger
except ImportError:
    def init_logger():
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - [%(levelname).1s] - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        return logging.getLogger()

# --------------------------------------------------------------------------- #
# 常量与枚举
# --------------------------------------------------------------------------- #


class CheckinStatus(Enum):
    """签到状态"""

    SUCCESS = 0
    REPEAT = 1
    FAILURE = -2


class APIEndpoint:
    """API 路径"""

    CHECKIN = "/api/user/checkin"
    CHECKIN_CHALLENGE = "/api/user/checkin/challenge"
    STATUS = "/api/user/status"
    POINTS = "/api/user/points"
    EXCHANGE = "/api/user/exchange"
    EXCHANGE_FALLBACK = "/api/user/points/exchange"


class LogEmoji:
    """日志 Emoji 常量"""

    SUCCESS = "✅"
    FAIL = "❌"
    REPEAT = "🔄"
    CHECKIN = "🎫"
    POINTS = "💰"
    EXCHANGE = "🎁"
    START = "🚀"
    END = "🏁"
    COOKIE = "🍪"
    DOMAIN = "🌐"
    WARNING = "⚠️"
    ERROR = "🔴"
    INFO = "ℹ️"


class ProbeResult(Enum):
    """单域名探测结果"""

    HIT = "hit"  # 命中登录状态
    NETWORK = "network"  # 无响应，值得重试
    REJECTED = "rejected"  # 有响应但被拒，重试无意义


"""签到状态 → (emoji, 文案)"""
STATUS_META = {
    CheckinStatus.SUCCESS: (LogEmoji.SUCCESS, "签到成功"),
    CheckinStatus.REPEAT: (LogEmoji.REPEAT, "重复签到"),
    CheckinStatus.FAILURE: (LogEmoji.ERROR, "签到失败"),
}


"""探测失败时的域名占位符"""
UNPROBED = "未探测"

"""剩余天数合理上限（约 100 年），超过视为异常数据"""
MAX_LEFT_DAYS = 36500

"""剩余天数低于此值时在总结里提醒续期"""
EXPIRY_WARN_DAYS = 7

"""积分查询超时（连接, 读取）。非关键请求且不重试；该接口实测 0.8~24s 波动较大，故放宽读取上限"""
POINTS_TIMEOUT = (10, 30)

"""单次运行总预算（秒）。到期后跳过剩余账号，避免 Actions 长时间挂起"""
RUN_BUDGET_SECONDS = 900

"""签到 challenge 的 proof 后缀与最小等待（与网页端当前实现一致）"""
CHALLENGE_SUFFIX = "glados-observe-v1"
CHALLENGE_DELAY_SECONDS = 2.5

"""Cookie 中标识登录态的会话字段"""
SESSION_FIELDS = ("koa:sess=", "gld:sess=")

"""各平台合法 User-Agent（版本号无关，仅平台 token 关键）"""
PLATFORM_UA = {
    "Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "macOS": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
    "Linux": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "iPhone": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "iOS": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Android": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
}

"""合法主机名：字母数字与连字符，点分"""
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

"""候选域名（探测顺序）：命中登录状态即锁定并只在该域名签到。各站 Cookie 通常不互通。"""
CANDIDATE_DOMAINS = [
    "glados.cloud",
    "railgun.info",
    "glados.network",
    "glados.rocks",
    "glados.one",
    "glados.space",
    "glados.vip",
    "glados-facility.com",
]

""""真值"型环境变量的可接受取值"""
TRUTHY = ("true", "1", "yes", "y")
FALSY = ("false", "0", "no", "n")

"""模块级 logger：只取记录器，不在此处配置全局 logging（配置交给 main / 宿主）"""
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 纯工具函数
# --------------------------------------------------------------------------- #


def plan_type_for_points(points: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
    """原项目逻辑：根据积分返回最大可兑换档位与标签"""
    if points is None:
        return None, None
    if points >= 500:
        return "plan500", "500->100 days"
    if points >= 200:
        return "plan200", "200->30 days"
    if points >= 100:
        return "plan100", "100->10 days"
    return None, None


def extract_points(payload: Any) -> Optional[int]:
    """递归从响应对象中提取总积分"""

    def _coerce(val):
        try:
            return int(float(val))
        except (TypeError, ValueError, OverflowError):
            return None

    def _walk(obj):
        if isinstance(obj, dict):
            for key in (
                "points",
                "point",
                "score",
                "balance",
                "totalPoints",
                "totalPoint",
                "total_points",
                "total",
                "leftPoints",
                "left_points",
                "remainPoints",
                "remain_points",
                "availablePoints",
                "available_points",
                "change",
            ):
                if key in obj and obj[key] is not None:
                    return obj[key]
            if "data" in obj:
                val = _walk(obj["data"])
                if val is not None:
                    return val
            for key in ("list", "items", "records"):
                if key in obj:
                    val = _walk(obj[key])
                    if val is not None:
                        return val
        elif isinstance(obj, list):
            for item in obj:
                val = _walk(item)
                if val is not None:
                    return val
        return None

    raw = _walk(payload)
    if raw is None:
        return None
    return _coerce(raw)


def to_int(value: Any) -> Optional[int]:
    """把 "20" / 20.0 / 20 转成 int；非法值、NaN、inf、bool 一律返回 None"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    try:
        return int(number)
    except (OverflowError, ValueError):
        return None


def left_days(data: Optional[dict]) -> Optional[int]:
    """从 /api/user/status 响应取剩余天数；取不到或异常值返回 None"""
    if not isinstance(data, dict):
        return None
    payload = data.get("data")
    days = to_int(payload.get("leftDays") if isinstance(payload, dict) else None)
    if days is None or abs(days) > MAX_LEFT_DAYS:
        return None
    return days


def account_name(data: Optional[dict]) -> str:
    """从状态响应中尽量提取账号名称，优先使用邮箱。"""
    if not isinstance(data, dict):
        return ""

    def _value(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.split())[:120]

    def _find(payload: Dict[str, Any]) -> str:
        for key in ("email", "mail", "username", "userName", "account", "name", "nickname"):
            value = _value(payload.get(key))
            if value:
                return value
        for key in ("user", "profile", "account", "userInfo", "data"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                value = _find(nested)
                if value:
                    return value
        return ""

    return _find(data)


def normalize_origin(raw: str) -> str:
    """把 origin 归一化为纯主机名（小写、去协议/路径/端口）。

    支持 `glados.cloud`、`https://glados.cloud`、`https://glados.cloud/console/checkin`；
    带路径时只取主机名，避免拼出 `host/path/api/user/status` 这类错误 URL。
    格式非法返回空串（等价于未填 origin，会回退遍历全部候选域名）。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    try:
        host = (urlsplit(text).hostname or "").strip().lower()
    except ValueError:
        return ""
    if not host or "." not in host or not _HOSTNAME_RE.match(host):
        return ""
    return host


def format_days(days: Optional[int]) -> str:
    """剩余天数 → 展示文本；取不到时显示 `None 天`"""
    return "None 天" if days is None else f"{days} 天"


def format_points(points: Optional[int]) -> str:
    """积分 → 展示文本；取不到时显示 `None 积分`"""
    return "None 积分" if points is None else f"{points} 积分"


def log_line(label: str, domain: str, emoji: str, message: str, enabled: bool = True) -> None:
    """统一的「账号 + 域名」日志前缀；enabled=False 时静默。

    日志级别跟随 emoji：ERROR→error，WARNING/FAIL→warning，其余→info，
    避免「错误内容却是 [I] 级别」这种级别与语义脱节。
    """
    if not enabled:
        return
    line = f"{LogEmoji.COOKIE}[{label}] {LogEmoji.DOMAIN}[{domain}] {emoji} {message}"
    if emoji == LogEmoji.ERROR:
        logger.error(line)
    elif emoji in (LogEmoji.WARNING, LogEmoji.FAIL):
        logger.warning(line)
    else:
        logger.info(line)


def safe_call(default: Any):
    """统一捕获 API 方法异常并返回安全默认值，避免单点异常中断整轮签到。

    默认值可以是值本身，也可以是接收异常的工厂函数。
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except Exception as e:  # noqa: BLE001 - 兜底，避免单个请求失败中断流程
                log_line(self.label, self.domain, LogEmoji.ERROR, f"{func.__name__} 执行失败: {e}")
                return default(e) if callable(default) else default

        return wrapper

    return decorator


class RunBudget:
    """整轮运行的时间预算：到期后跳过剩余账号，避免整轮卡死。"""

    def __init__(self, seconds: int = RUN_BUDGET_SECONDS):
        self.deadline = time.monotonic() + seconds

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass()
class CheckinResult:
    """单个账号的签到结果"""

    domain: str = UNPROBED
    points: int = 0  # 本次签到所得积分
    days: Optional[int] = None  # 剩余天数；None 表示未取到
    points_total: Optional[int] = None  # 账户总积分；None 表示未取到
    exchange: str = ""  # 仅在真正执行兑换时才有值，不含「未配置」这类配置状态
    note: str = ""  # 附加提示（如剩余天数偏少）
    code: CheckinStatus = CheckinStatus.FAILURE

    @classmethod
    def failure(cls, domain: str = UNPROBED) -> "CheckinResult":
        return cls(domain=domain)


@dataclass()
class CookieEntry:
    """一个账号：自己完成探测域名 → 签到 → 查积分 → 可选兑换，结果存在 self.result。"""

    index: int
    cookie: str
    name: str = ""
    origin: str = ""
    user_agent: str = ""  # 提取 Cookie 时的浏览器 UA；空表示用默认 UA
    verbose: bool = False
    result: CheckinResult = field(default_factory=CheckinResult)

    # ---------------- 构建 ----------------

    @classmethod
    def build(
        cls,
        index: int,
        cookie: str,
        name: str = "",
        origin: str = "",
        user_agent: str = "",
        verbose: bool = False,
    ) -> "CookieEntry":
        return cls(
            index=index,
            cookie=cookie,
            name=name or f"账号{index}",
            origin=origin,
            user_agent=(user_agent or "").strip(),
            verbose=verbose,
        )

    @staticmethod
    def has_session_field(cookie: str) -> bool:
        """Cookie 至少含一个会话字段才算有效账号"""
        return any(field in cookie for field in SESSION_FIELDS)

    # ---------------- 展示 ----------------

    @property
    def is_named(self) -> bool:
        return bool(self.name) and self.name != f"账号{self.index}"

    @property
    def label(self) -> str:
        """日志标识：优先显示配置或状态响应中的账号名。"""
        return self.name or f"账号{self.index}"

    def update_name(self, name: str) -> None:
        """未配置名称时，以服务端状态返回的账号名替代默认编号。"""
        if not self.is_named and name:
            self.name = name

    def log(self, domain: str, emoji: str, message: str, force: bool = False) -> None:
        """带账号与域名前缀的日志"""
        log_line(self.label, domain, emoji, message, self.verbose or force)

    # ---------------- 主流程 ----------------

    def checkin(self, config: "Config", budget: Optional[RunBudget] = None) -> CheckinResult:
        """执行签到，结果写入 self.result 并返回。"""
        self.verbose = config.verbose

        domain, days = self._resolve_domain(config, budget)
        if domain is None:
            self.result = CheckinResult.failure(self.origin or UNPROBED)
            if budget is not None and budget.expired():
                self.result.note = "运行超时，未完成探测"
            self._log_result()
            return self.result

        # 探测已耗尽预算时不再发起签到请求：单次签到最长可达 90s，会把整轮拖过预算
        if budget is not None and budget.expired():
            self.result = CheckinResult(domain=domain, days=days)
            self.result.note = "运行超时，已跳过签到"
            self.log(domain, LogEmoji.WARNING, "运行超出预算，跳过签到", force=True)
            self._log_result()
            return self.result

        with API(domain, self.cookie, label=self.label, verbose=config.verbose, user_agent=self.user_agent) as api:
            self.result = self._sign_in(api, domain, days, config)

        self._log_result()
        return self.result

    def _sign_in(self, api: "API", domain: str, days: Optional[int], config: "Config") -> CheckinResult:
        """签到 → 查状态/积分 → 在最后一天按最大天数自动兑换"""
        self.log(domain, LogEmoji.CHECKIN, "执行签到 (已启用网页端 challenge 对齐)")
        result = api.checkin()
        result.domain = domain
        result.days = days

        self.log(domain, LogEmoji.POINTS, "查询总积分")
        points = api.get_points()

        # 若未从 points 接口获取到，或天数未获取到，尝试从 status 获取
        if points is None or result.days is None:
            status_data = api.fetch_status()
            if result.days is None:
                result.days = left_days(status_data)
            if points is None and isinstance(status_data, dict):
                points = extract_points(status_data)

        result.points_total = points

        # 仅在签到成功或重复签到时执行自动兑换
        if result.code in (CheckinStatus.SUCCESS, CheckinStatus.REPEAT):
            result.exchange = self._apply_exchange(api, config, domain, result.days, result.points_total)
        else:
            self.log(domain, LogEmoji.INFO, "签到未成功，跳过自动兑换")

        if not result.note:
            result.note = self._expiry_note(result.days)
        return result

    def _apply_exchange(
        self,
        api: "API",
        config: "Config",
        domain: str,
        days: Optional[int],
        points: Optional[int],
    ) -> str:
        """原项目自动兑换逻辑：在最后一天 (0 <= 剩余天数 <= exchange_window_days) 兑换最大天数"""
        if not config.auto_exchange:
            self.log(domain, LogEmoji.INFO, "未开启自动兑换 (AUTO_EXCHANGE=0)，跳过兑换")
            return ""

        plan_type, exchange_label = plan_type_for_points(points)
        self.log(domain, LogEmoji.POINTS, f"当前总积分: {points} · 可兑换额度: {exchange_label or '无'}")

        if days is None:
            self.log(domain, LogEmoji.INFO, "未获取到剩余天数，跳过自动兑换")
            return ""

        in_exchange_window = (0 <= days <= config.exchange_window_days)

        if in_exchange_window and plan_type is None and isinstance(points, int):
            need = 100 - points
            if need > 0:
                msg = f"剩余天数: {days} 天 (已到最后一天)，当前积分 {points}，还差 {need} 积分达到 100 最低兑换额度"
                self.log(domain, LogEmoji.WARNING, msg, force=True)
                return msg

        if in_exchange_window and plan_type is not None:
            self.log(
                domain,
                LogEmoji.EXCHANGE,
                f"剩余天数: {days} 天 (进入最后一天窗口 <= {config.exchange_window_days} 天)，开始兑换最大天数: {exchange_label}",
                force=True,
            )
            return api.exchange(plan_type, exchange_label)

        if not in_exchange_window and plan_type is not None:
            self.log(
                domain,
                LogEmoji.INFO,
                f"跳过兑换: 剩余天数 {days} 天，未到最后一天 (需 0 <= 剩余天数 <= {config.exchange_window_days} 天)",
            )
            return ""

        return ""

    @staticmethod
    def _expiry_note(days: Optional[int]) -> str:
        """剩余天数偏少（或已过期）时提醒，避免服务悄悄到期"""
        if days is None:
            return ""
        if days < 0:
            return f"已过期 {abs(days)} 天，请及时续期"
        if days <= EXPIRY_WARN_DAYS:
            return f"仅剩 {days} 天，请留意到期；进入最后一天将自动按积分兑换最大档位"
        return ""

    def _log_result(self) -> None:
        """输出单账号结论：成功 ✅、重复 🔄、失败 ⚠️"""
        r = self.result
        status = STATUS_META.get(r.code, (LogEmoji.ERROR, "签到失败"))[1]
        parts = [f"结果: {status}"]
        if r.code in (CheckinStatus.SUCCESS, CheckinStatus.REPEAT) and self.verbose:
            parts += [f"获得 {r.points} 积分", f"剩余 {format_days(r.days)}", f"总 {format_points(r.points_total)}"]
            if r.exchange:
                parts.append(r.exchange)
        if r.code is CheckinStatus.SUCCESS:
            emoji = LogEmoji.SUCCESS
        elif r.code is CheckinStatus.REPEAT:
            emoji = LogEmoji.REPEAT
        else:
            emoji = LogEmoji.WARNING
        self.log(r.domain, emoji, ", ".join(parts), force=True)

    # ---------------- 域名探测 ----------------

    def _candidates(self, config: "Config") -> List[str]:
        """探测顺序：origin 优先（填错则回退），其余按配置顺序补齐"""
        ordered: List[str] = [self.origin] if self.origin else []
        ordered.extend(d for d in config.domains if d not in ordered)
        return ordered

    def _probe(self, domain: str, probing: bool, config: "Config") -> Tuple[ProbeResult, Optional[int], Optional[int]]:
        """探测单域名，返回 (结果类型, 剩余天数, code)。异常按网络问题处理，不中断整轮。"""
        try:
            with API(domain, self.cookie, label=self.label, verbose=config.verbose, probing=probing, user_agent=self.user_agent) as api:
                data = api.fetch_status()
        except Exception as e:  # noqa: BLE001 - 单域名失败不应影响其他域名
            logger.warning(f"{LogEmoji.WARNING} 探测 {domain} 时发生异常，按网络错误处理: {e}")
            return ProbeResult.NETWORK, None, None

        if data is None:
            return ProbeResult.NETWORK, None, None
        if not isinstance(data, dict):
            logger.warning(f"{LogEmoji.WARNING} 探测 {domain} 返回非对象响应（{type(data).__name__}），按被拒处理")
            return ProbeResult.REJECTED, None, -2

        code = data.get("code", -2)
        if code == 0 and isinstance(data.get("data"), dict):
            self.update_name(account_name(data))
            return ProbeResult.HIT, left_days(data), code
        return ProbeResult.REJECTED, None, code

    def _resolve_domain(self, config: "Config", budget: Optional[RunBudget] = None) -> Tuple[Optional[str], Optional[int]]:
        """探测账号实际所属域名，返回 (域名, 剩余天数)；全部失败返回 (None, None)。

        命中时的 /api/user/status 结果直接复用为签到前的状态查询，不额外增加请求。
        首轮短超时快筛，仅对「无响应」的域名用长超时重试一次；总预算到期即停止。
        """
        network: List[str] = []
        rejected: List[str] = []

        for domain in self._candidates(config):
            if budget is not None and budget.expired():
                self.log(self.origin or UNPROBED, LogEmoji.WARNING, "运行超出预算，停止探测剩余域名", force=True)
                break
            kind, days, code = self._probe(domain, True, config)
            if kind is ProbeResult.HIT:
                self.log(domain, LogEmoji.SUCCESS, f"命中登录域名（剩余 {format_days(days)}）", force=True)
                return domain, days
            if kind is ProbeResult.NETWORK:
                network.append(domain)
            else:
                rejected.append(f"{domain}(code={code})")

        if network:
            self.log(
                self.origin or UNPROBED,
                LogEmoji.WARNING,
                f"以下域名首轮无响应，用长超时重试一次：{'、'.join(network)}",
                force=True,
            )
            for domain in list(network):
                if budget is not None and budget.expired():
                    self.log(self.origin or UNPROBED, LogEmoji.WARNING, "运行超出预算，停止重试剩余域名", force=True)
                    break
                kind, days, code = self._probe(domain, False, config)
                if kind is ProbeResult.HIT:
                    self.log(domain, LogEmoji.SUCCESS, f"重试后命中登录域名（剩余 {format_days(days)}）", force=True)
                    return domain, days
                if kind is ProbeResult.REJECTED:
                    network.remove(domain)
                    rejected.append(f"{domain}(code={code})")

        if budget is not None and budget.expired():
            return None, None

        self._report_failure(network, rejected)
        return None, None

    def _report_failure(self, network: List[str], rejected: List[str]) -> None:
        """按失败原因分类输出，避免「网络故障 / Cookie 失效 / 域名不匹配」混为一谈"""
        domain = self.origin or UNPROBED
        if rejected and not network:
            self.log(
                domain,
                LogEmoji.ERROR,
                f"候选域名均返回「没有权限」：{'、'.join(rejected)}。"
                f"可能已在别处退出登录 / 改密导致 Cookie 失效，或账号属于未列入候选的域名"
                f"——请重新登录后更新 Cookie",
                force=True,
            )
        elif network and not rejected:
            self.log(
                domain,
                LogEmoji.ERROR,
                f"候选域名均未获得服务端响应（{'、'.join(network)}）：网络或服务端故障，非 Cookie 问题，建议稍后重跑",
                force=True,
            )
        else:
            self.log(
                domain,
                LogEmoji.ERROR,
                f"探测失败：网络无响应 {'、'.join(network) or '无'}；返回无权限 {'、'.join(rejected) or '无'}",
                force=True,
            )


@dataclass()
class Accounts:
    """账号集合：解析固定的 JSON 数组格式并去重"""

    entries: List[CookieEntry] = field(default_factory=list)

    """账号对象允许的字段；cookie 必填，其余可省略或留空"""
    FIELDS = ("name", "origin", "cookie", "user-agent")

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)

    # ---------------- 解析 ----------------

    @classmethod
    def parse(cls, raw: str, verbose: bool = False) -> "Accounts":
        """解析 GLADOS_COOKIES / GLADOS_COOKIE。

        推荐格式：JSON 数组，每项为账号对象：
            [{"name": "", "origin": "", "cookie": "...", "user-agent": ""}]

        cookie 必填且不能为空；name / origin / user-agent 可省略或留空。
        另外兼容旧版 `&` 分隔格式，但每次解析都会提示尽快迁移。
        """
        text = (raw or "").lstrip("﻿").strip()
        if not text:
            raise ValueError("GLADOS_COOKIES / GLADOS_COOKIE 为空")

        if text.startswith(("[", "{")):
            return cls._parse_json(text, verbose)

        return cls._parse_legacy(text, verbose)

    @classmethod
    def _parse_json(cls, text: str, verbose: bool) -> "Accounts":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"不是合法 JSON（只支持 JSON 数组格式）：{e}") from e

        if isinstance(parsed, dict):
            parsed = [parsed]
        elif not isinstance(parsed, list):
            raise ValueError("只支持 JSON 数组格式：[{...}, {...}]")
        if not parsed:
            raise ValueError("账号数组为空")

        items = [cls._parse_item(item, position) for position, item in enumerate(parsed, 1)]
        return cls(cls._dedupe(items, verbose))

    @classmethod
    def _parse_legacy(cls, text: str, verbose: bool) -> "Accounts":
        """兼容旧版 `&` 分隔格式；换行分隔已不支持。"""
        items: List[Tuple[str, str, str, str]] = []
        for position, part in enumerate((seg.strip() for seg in text.split("&")), 1):
            if not part:
                continue
            if "\n" in part:
                raise ValueError("换行分隔格式已不支持，请改用 JSON 数组或 & 分隔")
            if not CookieEntry.has_session_field(part):
                logger.warning(f"{LogEmoji.WARNING} 第 {position} 段缺少 koa:sess / gld:sess，已跳过")
                continue
            items.append(("", "", part, ""))

        if not items:
            raise ValueError("未解析到任何有效账号")

        logger.warning(f"{LogEmoji.WARNING} 检测到旧版 & 分隔格式，建议尽快更新为固定 JSON 格式（见 README）")
        return cls(cls._dedupe(items, verbose))

    @classmethod
    def _parse_item(cls, item: Any, position: int) -> Tuple[str, str, str, str]:
        """解析单个账号对象，返回 (name, origin, cookie, user_agent)"""
        if not isinstance(item, dict):
            raise ValueError(f'第 {position} 项必须是对象，例如 {{"cookie": "..."}}')

        unknown = [key for key in item if key not in cls.FIELDS]
        if unknown:
            raise ValueError(f"第 {position} 项含未知字段 {unknown}，只允许 {list(cls.FIELDS)}")

        cookie = str(item.get("cookie") or "").strip()
        if not cookie:
            raise ValueError(f"第 {position} 项缺少 cookie（必填且不能为空）")
        if not CookieEntry.has_session_field(cookie):
            raise ValueError(f"第 {position} 项的 Cookie 缺少 koa:sess / gld:sess，请整段复制完整 Cookie")

        return (
            str(item.get("name") or "").strip(),
            normalize_origin(str(item.get("origin") or "")),
            cookie,
            " ".join(str(item.get("user-agent") or "").split()),
        )

    @staticmethod
    def _dedupe(items: List[Tuple[str, str, str, str]], verbose: bool) -> List[CookieEntry]:
        """构建账号并对重复 Cookie 去重；同一 Cookie 只跑一次，避免重复签到与重复推送"""
        entries: List[CookieEntry] = []
        seen: Dict[str, str] = {}
        for name, origin, cookie, user_agent in items:
            if cookie in seen:
                logger.warning(f"{LogEmoji.WARNING} 检测到重复的 Cookie（与 {seen[cookie]} 相同），已跳过（name={name or '未命名'}）")
                continue
            entry = CookieEntry.build(len(entries) + 1, cookie, name, origin, user_agent, verbose)
            seen[cookie] = entry.label
            entries.append(entry)
        return entries


# --------------------------------------------------------------------------- #
# 签到响应解析
# --------------------------------------------------------------------------- #


def earned_points(raw: Dict) -> int:
    """本次签到所得积分"""
    points = to_int(raw.get("points"))
    if points is not None:
        return points

    records = raw.get("list")
    if isinstance(records, list) and records and isinstance(records[0], dict):
        change = to_int(records[0].get("change"))
        if change is not None:
            return change
    return 0


def log_reject(code: int, raw: Dict, message: str, domain: str, log: Callable[..., None], ua_hint: str = "") -> None:
    """失败分支按 code 细分诊断，便于快速定位根因"""
    if code == 4:
        device = raw.get("loginDevice") or "未知"
        if raw.get("reason") == "device-mismatch":
            if device in PLATFORM_UA:
                log(LogEmoji.ERROR, f"设备平台不匹配(loginDevice={device})，重试后仍失败，请确认登录设备平台", True)
            else:
                log(LogEmoji.ERROR, f"设备平台不匹配(loginDevice={device} 不在支持列表)，无法自动切换 UA", True)
        else:
            log(LogEmoji.ERROR, f"服务端拒绝签到(code=4, reason={raw.get('reason')})，请检查账户风控状态", True)
        if ua_hint:
            log(LogEmoji.INFO, f"建议：{ua_hint}", True)
    elif code == -2:
        log(
            LogEmoji.ERROR,
            f"无权限(code=-2)：Cookie 可能已失效，请重新登录 {domain} 刷新 Cookie 并更新 Secret GLADOS_COOKIES / GLADOS_COOKIE",
            True,
        )
    else:
        log(LogEmoji.ERROR, f"未预期响应(code={code}, message={message})，请检查 GLaDOS 服务状态或 Cookie", True)


def parse_checkin(raw: Optional[Dict], domain: str, log: Callable[..., None], ua_hint: str = "") -> CheckinResult:
    """把签到响应解析为 CheckinResult（只填 domain/points/code）；log 只用于诊断。"""
    result = CheckinResult(domain=domain)
    if not isinstance(raw, dict):
        if raw is not None:
            log(LogEmoji.ERROR, f"签到响应格式异常（{type(raw).__name__}）", True)
        return result

    code = raw.get("code")
    if isinstance(code, str) and code.strip().isdigit():
        code = int(code.strip())
    message = str(raw.get("message", "无消息字段"))
    points = earned_points(raw)

    # token 与域名不一致时服务端返回 "please checkin via https://..."，此响应 code 可能为 0
    if "please checkin via" in message.lower():
        log(LogEmoji.ERROR, f"签到被拒：{message}（服务端认定签到域名不符）", True)
        return result

    # ① 设备不匹配
    if code == 4 and raw.get("reason") == "device-mismatch":
        log_reject(4, raw, message, domain, log, ua_hint)
        result.note = "登录设备不匹配，需在本服务的 Chromium 内重新登录"
        return result

    # ②~④ ：repeated「已签到」，code==0 成功
    repeated = raw.get("repeated")
    if isinstance(repeated, bool):
        if repeated:
            status = CheckinStatus.REPEAT
        else:
            status = CheckinStatus.SUCCESS if code == CheckinStatus.SUCCESS.value else CheckinStatus.FAILURE
    elif code == CheckinStatus.SUCCESS.value:
        status = CheckinStatus.SUCCESS
    elif code == CheckinStatus.REPEAT.value:
        status = CheckinStatus.REPEAT
    elif raw.get("checkedIn") is True:
        status = CheckinStatus.REPEAT
    else:
        status = CheckinStatus.FAILURE

    if status is CheckinStatus.SUCCESS:
        log(LogEmoji.SUCCESS, f"{{ code : {code}, points : {points}, message : {message} }}")
        result.points = points
        result.code = CheckinStatus.SUCCESS
    elif status is CheckinStatus.REPEAT:
        log(LogEmoji.REPEAT, f"{{ code : {code}, repeated : {repeated}, message : {message} }}", True)
        result.code = CheckinStatus.REPEAT
    elif isinstance(code, int) and not isinstance(code, bool):
        log_reject(code, raw, message, domain, log, ua_hint)
    else:
        log(LogEmoji.ERROR, f"签到响应缺少 code 字段，无法判定结果（message={message}）", True)
    return result


# --------------------------------------------------------------------------- #
# API：绑定 (域名, Cookie) 的单站点会话
# --------------------------------------------------------------------------- #


class API:
    """针对单个域名、单个 Cookie 的 API 客户端。

    实例即一次会话：域名与 Cookie 在构造时绑定，方法无需再传。
    probing=True 用于域名探测：短超时 + 单次重试，避免多域名叠加成十几分钟。
    """

    def __init__(
        self,
        domain: str,
        cookie: str,
        label: str = "",
        verbose: bool = False,
        probing: bool = False,
        user_agent: str = "",
    ):
        self.domain = domain
        self.cookie = cookie
        self.label = label or "-"
        self.verbose = verbose
        self.probing = probing
        self.user_agent = (user_agent or "").strip()

        if probing:
            self.timeout: Tuple[int, int] = (5, 10)
            retry = Retry(total=1, backoff_factor=0.3, raise_on_status=False)
        else:
            self.timeout = (15, 30)
            retry = Retry(
                total=2,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=("GET", "POST"),
                raise_on_status=False,
            )

        self.headers = {
            "origin": f"https://{domain}",
            "referer": f"https://{domain}/console/checkin",
            "user-agent": self.user_agent or PLATFORM_UA["Windows"],
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # 非幂等/非关键请求走这个 session：不重试（兑换不能重放）
        self._no_retry_session = requests.Session()
        self._no_retry_session.headers.update(self.headers)
        no_retry_adapter = HTTPAdapter(max_retries=Retry(total=0))
        self._no_retry_session.mount("https://", no_retry_adapter)
        self._no_retry_session.mount("http://", no_retry_adapter)

    # ---------------- 生命周期 ----------------

    def __enter__(self) -> "API":
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False

    def close(self) -> None:
        for session in (self.session, self._no_retry_session):
            try:
                session.close()
            except Exception as e:  # noqa: BLE001
                logger.error(f"{LogEmoji.ERROR} 关闭 session 时发生错误: {e}")

    # ---------------- 对外接口 ----------------

    @staticmethod
    def _is_device_mismatch(raw: Optional[Dict]) -> bool:
        return isinstance(raw, dict) and raw.get("code") == 4 and raw.get("reason") == "device-mismatch"

    @safe_call(default=lambda e: CheckinResult())
    def checkin(self) -> CheckinResult:
        """执行签到。未指定 user-agent 时，设备不匹配会按 loginDevice 自适应重试一次。"""
        raw = self._checkin_attempt(self.headers["user-agent"])
        hint = ""

        if self._is_device_mismatch(raw):
            device = raw.get("loginDevice")

            if self.user_agent:
                hint = f"已使用配置的 user-agent，但仍与登录设备({device})不一致；请改为填写提取 Cookie 时浏览器的 User-Agent"
            else:
                recover_ua = PLATFORM_UA.get(device)
                if recover_ua:
                    self.log(LogEmoji.WARNING, f"设备平台不匹配 (loginDevice={device})，切换 UA 重试", force=True)
                    raw = self._checkin_attempt(recover_ua)
                if self._is_device_mismatch(raw):
                    hint = "自动切换 UA 后仍不匹配；请在账号 JSON 中补充 `user-agent` 字段，填入提取 Cookie 时浏览器的 User-Agent"

        return parse_checkin(raw, self.domain, self.log, hint)

    @safe_call(default=None)
    def fetch_status(self) -> Optional[dict]:
        """请求 /api/user/status，返回原始 JSON；失败返回 None"""
        response = self.request("GET", APIEndpoint.STATUS)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            self.log(LogEmoji.ERROR, "状态响应解析失败", force=True)
            return None

    def _json_dict(self, response: requests.Response, what: str) -> Optional[Dict]:
        """解析 JSON 并校验为对象；失败时记录日志并返回 None。"""
        try:
            data = response.json()
        except ValueError:
            self.log(LogEmoji.ERROR, f"{what}响应解析失败（非 JSON）", force=True)
            return None
        if not isinstance(data, dict):
            self.log(LogEmoji.ERROR, f"{what}响应格式异常", force=True)
            return None
        return data

    @safe_call(default=None)
    def get_points(self) -> Optional[int]:
        """获取总积分。非关键请求，用短超时快速失败；取不到返回 None。"""
        response = self.request("GET", APIEndpoint.POINTS, timeout=POINTS_TIMEOUT, retry=False)
        if response is None:
            return None

        data = self._json_dict(response, "积分")
        if data is None:
            return None

        points = to_int(data.get("points"))
        if points is None:
            self.log(LogEmoji.ERROR, f"积分字段无法解析: {data.get('points')!r}", force=True)
            return None

        self.log(LogEmoji.SUCCESS, f"{{ code : {data.get('code', -2)}, points : {points} 积分}}")
        return points

    @safe_call(default="兑换未完成，请到站点确认")
    def exchange(self, plan: str, label: str = "") -> str:
        """执行积分兑换。遍历候选端点尝试兑换"""
        endpoints = [
            APIEndpoint.EXCHANGE,
            APIEndpoint.EXCHANGE_FALLBACK,
            "/api/user/points/convert",
            "/api/user/points/redeem",
        ]
        # 支持通过环境变量自定义兑换端点
        custom_endpoints = []
        env = os.environ.get("GLADOS_EXCHANGE_URLS") or os.environ.get("GLADOS_EXCHANGE_URL") or ""
        if env:
            for p in env.replace(";", ",").split(","):
                p = p.strip()
                if not p:
                    continue
                if p.startswith("http://") or p.startswith("https://"):
                    try:
                        custom_endpoints.append(urlsplit(p).path)
                    except ValueError:
                        pass
                else:
                    custom_endpoints.append(p if p.startswith("/") else f"/{p}")

        all_endpoints = custom_endpoints + [ep for ep in endpoints if ep not in custom_endpoints]
        data = None

        for endpoint in all_endpoints:
            response = self.request("POST", endpoint, data={"planType": plan}, retry=False, json_body=True)
            if response is None:
                response = self.request("POST", endpoint, data={"planType": plan}, retry=False, json_body=False)
            if response is None:
                continue

            data = self._json_dict(response, "兑换")
            if data is not None:
                break

        tag = label or plan
        if data is None:
            self.log(LogEmoji.WARNING, f"兑换 {tag} 未完成（接口无有效响应），请到站点确认", force=True)
            return f"exchange {tag}--未完成"

        code = data.get("code", -2)
        message = data.get("message") or data.get("msg") or str(data)
        if code == 0:
            self.log(LogEmoji.SUCCESS, f"exchange {tag}--{message}")
            return f"exchange {tag}--{message}"

        self.log(LogEmoji.FAIL, f"exchange {tag}--{message}", force=True)
        return f"exchange {tag}--{message}"

    def log(self, emoji: str, message: str, force: bool = False) -> None:
        log_line(self.label, self.domain, emoji, message, self.verbose or force)

    # ---------------- 内部实现 ----------------

    def request(
        self,
        method: str,
        path: str,
        data: Optional[Dict] = None,
        user_agent: Optional[str] = None,
        timeout: Optional[Tuple[int, int]] = None,
        retry: bool = True,
        json_body: bool = False,
    ) -> Optional[requests.Response]:
        """发送请求。retry=False 用于非幂等/非关键请求，跳过 adapter 重试。"""
        url = f"https://{self.domain}{path}"
        headers = {**self.headers, "cookie": self.cookie}
        if user_agent:
            headers["user-agent"] = user_agent

        try:
            session = self.session if retry else self._no_retry_session
            if method.upper() == "POST":
                if json_body:
                    response = session.post(url, headers=headers, json=data, timeout=timeout or self.timeout)
                else:
                    response = session.post(url, headers=headers, data=data, timeout=timeout or self.timeout)
            elif method.upper() == "GET":
                response = session.get(url, headers=headers, timeout=timeout or self.timeout)
            else:
                self.log(LogEmoji.ERROR, f"不支持的 HTTP 方法: {method}", force=True)
                return None
        except requests.exceptions.RequestException as e:
            self.log(LogEmoji.ERROR, f"向 {url} 发起请求时发生网络错误: {e}", force=True)
            return None

        if not response.ok:
            self._log_failure(url, response)
            return None
        return response

    def _log_failure(self, url: str, response: requests.Response) -> None:
        """非 2xx 日志：5xx 只报状态码（body 多为网关 HTML 错误页，无信息量）"""
        status = response.status_code
        if status >= 500:
            self.log(
                LogEmoji.WARNING,
                f"向 {url} 请求失败，状态码 {status}（服务端错误，非 Cookie/客户端问题，多为临时抖动，重试后仍失败）",
                force=True,
            )
            return

        body = response.text or ""
        is_html = "html" in response.headers.get("Content-Type", "").lower() or body.lstrip()[:1] == "<"
        snippet = "（HTML 响应，已省略）" if is_html else body[:300] + ("..." if len(body) > 300 else "")
        self.log(LogEmoji.WARNING, f"向 {url} 请求失败，状态码 {status}。响应: {snippet}", force=True)

    def _checkin_challenge(self, user_agent: str) -> Optional[Tuple[str, str]]:
        """获取签到 challenge 并计算 proof；失败返回 None，由调用方回退旧协议。"""
        response = self.request("GET", APIEndpoint.CHECKIN_CHALLENGE, user_agent=user_agent)
        if response is None:
            return None
        try:
            data = response.json()
        except ValueError:
            self.log(LogEmoji.WARNING, "签到 challenge 响应解析失败，按旧协议提交", force=True)
            return None
        if not isinstance(data, dict) or data.get("code") != 0:
            self.log(LogEmoji.WARNING, f"签到 challenge 响应异常: {data}", force=True)
            return None

        nonce = data.get("nonce")
        if not nonce:
            self.log(LogEmoji.WARNING, "签到 challenge 缺少 nonce，按旧协议提交", force=True)
            return None

        # challenge 到手后等待间隔后再提交，模拟真实页面操作
        time.sleep(CHALLENGE_DELAY_SECONDS)
        proof = hashlib.sha256(f"{nonce}.{CHALLENGE_SUFFIX}".encode()).hexdigest()
        self.log(LogEmoji.INFO, "已获取签到 challenge")
        return str(nonce), proof

    def _checkin_attempt(self, user_agent: str) -> Optional[Dict]:
        """发起一次签到请求并返回解析后的响应体；请求失败或响应非 JSON 返回 None"""
        payload: Dict[str, str] = {"token": self.domain}
        challenge = self._checkin_challenge(user_agent)
        if challenge:
            nonce, proof = challenge
            payload.update({"nonce": nonce, "proof": proof})
        else:
            self.log(
                LogEmoji.WARNING,
                "未取得签到 challenge，按旧协议提交（可能只获得 1 积分）",
                force=True,
            )

        response = self.request(
            "POST",
            APIEndpoint.CHECKIN,
            data=payload,
            user_agent=user_agent,
            json_body=bool(challenge),
        )
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            self.log(LogEmoji.ERROR, "签到响应解析失败", force=True)
            return None


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class Config:
    """应用配置（来自环境变量）"""

    ENV_COOKIES = "GLADOS_COOKIES"
    ENV_COOKIE_LEGACY = "GLADOS_COOKIE"
    ENV_AUTO_EXCHANGE = "AUTO_EXCHANGE"
    ENV_AUTO_EXCHANGE_LEFT_DAYS = "AUTO_EXCHANGE_LEFT_DAYS"
    ENV_VERBOSE = "GLADOS_VERBOSE"
    ENV_BASE_URLS = "GLADOS_BASE_URLS"
    ENV_BASE_URL = "GLADOS_BASE_URL"

    DEFAULT_VERBOSE = False
    DEFAULT_AUTO_EXCHANGE = True
    DEFAULT_EXCHANGE_WINDOW_DAYS = 1.0

    def __init__(self, env: Optional[Dict[str, str]] = None):
        self.env = env if env is not None else os.environ
        self.accounts = Accounts()
        self.auto_exchange = self.DEFAULT_AUTO_EXCHANGE
        self.exchange_window_days = self.DEFAULT_EXCHANGE_WINDOW_DAYS
        self.verbose = self.DEFAULT_VERBOSE
        self.custom_domains: List[str] = []
        self.load_error = ""
        self._load()

    @property
    def domains(self) -> List[str]:
        domains = list(self.custom_domains)
        for d in CANDIDATE_DOMAINS:
            if d not in domains:
                domains.append(d)
        return domains

    # ---------------- 加载 ----------------

    def _load(self) -> None:
        self._load_verbose()
        self._load_custom_domains()
        self._load_accounts()
        self._load_auto_exchange()

        logger.info(f"{LogEmoji.INFO} 共加载了 {len(self.accounts)} 个账号用于签到。")
        logger.info(f"{LogEmoji.INFO} 自动兑换 {self.ENV_AUTO_EXCHANGE}: {self.auto_exchange} (最后期限窗口: <= {self.exchange_window_days} 天)。")
        logger.info(f"{LogEmoji.INFO} 详细日志 {self.ENV_VERBOSE}: {self.verbose}。")

    def _get(self, key: str) -> str:
        return (self.env.get(key) or "").strip()

    def _load_custom_domains(self) -> None:
        raw = self._get(self.ENV_BASE_URLS) or self._get(self.ENV_BASE_URL)
        if not raw:
            return
        for part in raw.replace(";", ",").split(","):
            host = normalize_origin(part)
            if host and host not in self.custom_domains:
                self.custom_domains.append(host)

    def _load_accounts(self) -> None:
        raw = self._get(self.ENV_COOKIES) or self._get(self.ENV_COOKIE_LEGACY)
        if not raw:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_COOKIES}' 或 '{self.ENV_COOKIE_LEGACY}' 未设置。")
            self.load_error = f"环境变量 {self.ENV_COOKIES} 未设置"
            return

        try:
            self.accounts = Accounts.parse(raw, self.verbose)
        except ValueError as e:
            logger.error(f"{LogEmoji.ERROR} Cookie 解析失败：{e}")
            self.load_error = f"Cookie 解析失败：{e}"

    def _load_auto_exchange(self) -> None:
        raw_auto = self._get(self.ENV_AUTO_EXCHANGE).lower()
        if raw_auto in FALSY:
            self.auto_exchange = False
        elif raw_auto in TRUTHY:
            self.auto_exchange = True

        raw_window = self._get(self.ENV_AUTO_EXCHANGE_LEFT_DAYS)
        if raw_window:
            try:
                self.exchange_window_days = float(raw_window)
            except ValueError:
                self.exchange_window_days = self.DEFAULT_EXCHANGE_WINDOW_DAYS

    def _load_verbose(self) -> None:
        value = self._get(self.ENV_VERBOSE).lower()
        if not value:
            return
        if value in TRUTHY:
            self.verbose = True
        elif value in FALSY:
            self.verbose = False
        else:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_VERBOSE}' 的值 '{value}' 无效，将使用默认值 {self.DEFAULT_VERBOSE}。")


# --------------------------------------------------------------------------- #
# 报告与推送
# --------------------------------------------------------------------------- #


class Report:
    """一次运行的结果视图：直接读 entry.result，渲染时才生成文本"""

    def __init__(self, accounts: Accounts, error: str = ""):
        self.entries = list(accounts)
        self.error = error

    @classmethod
    def failed(cls, error: str) -> "Report":
        return cls(Accounts(), error=error)

    @property
    def title(self) -> str:
        success = sum(1 for e in self.entries if e.result.code is CheckinStatus.SUCCESS)
        repeat = sum(1 for e in self.entries if e.result.code is CheckinStatus.REPEAT)
        failed = sum(1 for e in self.entries if e.result.code is CheckinStatus.FAILURE)

        if self.error:
            if not self.entries:
                return "# 脚本执行出错"
            return f"# 脚本执行出错 · 成功 {success} · 失败 {failed} · 重复 {repeat}"
        return f"GLaDOS 签到 · 成功 {success} · 失败 {failed} · 重复 {repeat}"

    def render(self, line_sep: str = "\n") -> str:
        """渲染签到结果正文。"""
        show_name = len(self.entries) > 1 and any(entry.name for entry in self.entries)
        lines = []
        for entry in self.entries:
            r = entry.result
            emoji, status = STATUS_META.get(r.code, (LogEmoji.ERROR, "签到失败"))
            name_cell = f"{entry.name or '未命名'}  " if show_name else ""
            line = (
                f"#{entry.index}  {name_cell}{emoji} {status}  {r.domain}  "
                f"获得 {r.points} 积分 · 剩余 {format_days(r.days)} · 总 {format_points(r.points_total)}"
            )
            if r.exchange:
                line += f" · {r.exchange}"
            if r.note:
                line += f"  ⚠️ {r.note}"
            lines.append(line)

        if self.error:
            lines.append(self.error)
        return line_sep.join(lines)


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #


class CheckinRunner:
    """按顺序驱动每个账号完成签到，产出 Report"""

    def __init__(self, config: Config):
        self.config = config

    def run(self, budget: Optional[RunBudget] = None) -> Report:
        accounts = self.config.accounts
        candidates = "、".join(self.config.domains)
        logger.info(f"{LogEmoji.INFO} 共 {len(accounts)} 个账号，候选域名 {len(self.config.domains)} 个（{candidates}）")

        budget = budget or RunBudget()
        for position, entry in enumerate(accounts, 1):
            if budget.expired():
                entry.result = CheckinResult.failure(entry.origin or UNPROBED)
                entry.result.note = "运行超时，已跳过"
                entry.log(entry.origin or UNPROBED, LogEmoji.WARNING, "运行超出预算，跳过该账号", force=True)
                continue
            logger.info(f"{LogEmoji.START} ========== 处理账号 {position}/{len(accounts)}：{entry.label} ==========")
            try:
                entry.checkin(self.config, budget)
            except Exception as e:  # noqa: BLE001
                entry.result = CheckinResult.failure(entry.origin or UNPROBED)
                entry.result.note = f"账号处理异常: {e}"
                entry.log(entry.origin or UNPROBED, LogEmoji.ERROR, f"处理异常，已跳过: {e}", force=True)

        return Report(accounts)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main() -> None:
    """主流程：加载配置 → 签到 → 渲染报告"""
    init_logger()
    exit_code = 0
    config: Optional[Config] = None
    report = Report.failed("未知错误")

    try:
        logger.info(f"{LogEmoji.START} 步骤 1: 加载配置")
        config = Config()
        report = Report.failed(config.load_error or "未找到有效 Cookie")

        if not config.accounts:
            logger.error(f"{LogEmoji.ERROR} {config.load_error or '未找到有效的 Cookie'}，退出程序。")
            exit_code = 1
        else:
            logger.info(f"{LogEmoji.START} 步骤 2: 执行签到")
            report = CheckinRunner(config).run()

            logger.info(f"{LogEmoji.START} 步骤 3: 生成报告")
            logger.info(f"\n{LogEmoji.END} ========== 签到总结 ==========\n{report.title}\n{report.render()}")

    except Exception as e:  # noqa: BLE001
        logger.error(f"{LogEmoji.ERROR} 主程序执行过程中发生未预期的错误: {e}")
        report = Report(config.accounts, error=str(e)) if config is not None and config.accounts else Report.failed(str(e))
        exit_code = 1

    logger.info(f"{LogEmoji.END} 签到完成")
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
