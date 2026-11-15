import logging
import logging.config
import datetime
import os
import re
import threading
from pathlib import Path


"""北京时间（UTC+8）"""
BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))


LEVEL_TAGS = {
    "DEBUG": "[D]",
    "INFO": "[I]",
    "WARNING": "[W]",
    "ERROR": "[E]",
    "CRITICAL": "[C]",
}

"""日志脱敏规则：抹掉 GLaDOS 会话字段与 Bearer 凭证，避免 Cookie 泄漏到 Actions 日志"""
_SECRET_PATTERNS = (
    (re.compile(r"\b((?:koa|gld):[A-Za-z0-9_.-]+=)([^;\s\"']+)"), r"\1[REDACTED]"),
    (re.compile(r"(\"cookie\"\s*:\s*\")([^\"]+)(\")", re.IGNORECASE), r"\1[REDACTED]\3"),
    (re.compile(r"(Bearer\s+)([A-Za-z0-9._~+/=-]+)", re.IGNORECASE), r"\1[REDACTED]"),
    # Do not expose proxy credentials in network exceptions or diagnostic logs.
    (re.compile(r"((?:https?|socks5h?|socks)://)([^\s/@:]+)(?::[^\s/@]*)?@", re.IGNORECASE), r"\1[REDACTED]@"),
)


def redact(text):
    """对日志文本做敏感信息脱敏"""
    if not isinstance(text, str) or not text:
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# 单个历史文件的上限。超出后丢弃最旧的完整行，保留近期记录。
LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[([A-Z]+)\] (.*)$")
_log_file_lock = threading.Lock()


def log_file_path() -> Path:
    """持久化日志位于数据卷，容器重建后仍然保留。"""
    data = Path(os.environ.get("GLADOS_DATA_DIR", Path(__file__).resolve().parent / "data"))
    return data / "logs" / "glados.log"


def append_persistent_log(message: str, level: str = "INFO") -> str:
    """追加一条已脱敏日志，并在超过上限时裁掉文件头部。"""
    now = datetime.datetime.now(BEIJING_TZ)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [{str(level or 'INFO').upper()}] {redact(str(message))}\n"
    path = log_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = line.encode("utf-8")
    with _log_file_lock:
        with path.open("ab") as handle:
            handle.write(data)
        size = path.stat().st_size
        if size > LOG_MAX_BYTES:
            _trim_log_file(path, size)
    return stamp


def _trim_log_file(path: Path, size: int) -> None:
    with path.open("rb") as handle:
        handle.seek(max(0, size - LOG_MAX_BYTES))
        kept = handle.read()
    newline = kept.find(b"\n")
    if newline != -1:
        kept = kept[newline + 1 :]
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(kept)
    temporary.replace(path)


def _iter_log_entries(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line:
                continue
            matched = _LOG_LINE.match(line)
            if matched:
                stamp, level, message = matched.groups()
                yield {"time": stamp, "level": level, "message": message}
            else:
                yield {"time": "", "level": "INFO", "message": line}


def read_recent_logs(count: int = 500) -> list[dict]:
    """读取历史文件末尾，供页面重启后恢复最近的日志。"""
    path = log_file_path()
    if count <= 0 or not path.exists():
        return []
    recent: list[dict] = []
    try:
        for entry in _iter_log_entries(path):
            recent.append(entry)
            if len(recent) > count:
                del recent[:-count]
    except OSError:
        return []
    for index, entry in enumerate(recent, 1):
        entry["id"] = index
    return recent


def query_persistent_logs(keyword: str = "", limit: int = 1000) -> dict:
    """从历史文件检索日志。返回条数有上限，完整内容通过下载获取。"""
    path = log_file_path()
    if limit <= 0:
        limit = 1000
    limit = min(limit, 1000)
    needle = str(keyword or "").strip().lower()
    matched: list[dict] = []
    total = 0
    try:
        for entry in _iter_log_entries(path):
            if needle and needle not in f"{entry['time']} {entry['level']} {entry['message']}".lower():
                continue
            total += 1
            matched.append(entry)
            if not needle and len(matched) > limit:
                del matched[:-limit]
    except OSError:
        matched = []
        total = 0
    shown = matched[-limit:]
    try:
        size = path.stat().st_size if path.exists() else 0
    except OSError:
        size = 0
    return {
        "total": total,
        "shown": len(shown),
        "size": size,
        "max_bytes": LOG_MAX_BYTES,
        "logs": shown,
    }


class AlignedFormatter(logging.Formatter):
    """日志级别显示为固定 3 字符短标签（[W]/[I]/[E]...）；时间用北京时间，并统一脱敏"""

    def formatTime(self, record, datefmt=None):
        """北京时间。strftime 不认 %f，这里统一走 datetime 以保证时区准确"""
        dt = datetime.datetime.fromtimestamp(record.created, tz=BEIJING_TZ)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")

    def format(self, record):
        # 复制一份再改写：直接改 record 会让后续 handler 读到已带标签的 levelname（输出变成 "[["），
        # 也会污染共享的 LogRecord。makeLogRecord 只做浅拷贝，开销可忽略。
        copy = logging.makeLogRecord(record.__dict__)
        copy.levelname = LEVEL_TAGS.get(record.levelname, f"[{record.levelname[0]}]")
        copy.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                copy.args = tuple(redact(arg) if isinstance(arg, str) else arg for arg in record.args)
            elif isinstance(record.args, dict):
                copy.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return super().format(copy)


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "()": "logging_config.AlignedFormatter",
            "format": "%(asctime)s - %(levelname)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "level": "INFO",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}


def init_logger():
    """初始化日志"""
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    logging.config.dictConfig(LOGGING_CONFIG)
    return logging.getLogger()
