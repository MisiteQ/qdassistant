#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用工具：时间 / 文件 / HTTP。

HTTP 响应归一语义：

- HTTP 2xx：解析 body 为 JSON；
- HTTP 错误：body 可解析则返回其 JSON，否则 `{"code": <status>, "message": <body 前 500 字符>}`；
- 网络错误：`{"code": -1, "message": <原因>}`；
- 直连、不读系统/环境代理。

NAS 部署时上游接口都在公网，走本机代理既没必要也容易被拦截，因此这里显式
构造一个禁用代理的 opener。
"""

from __future__ import annotations

import json
import os
import random
import secrets
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

# 默认 UA
DEFAULT_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

DEFAULT_HTTP_TIMEOUT = 30

# 显式禁用代理：`build_opener(ProxyHandler({}))` 会覆盖环境变量里的 http_proxy。
# NAS 常驻场景下系统代理不可控，统一直连，避免签到请求被中间人改写或超时。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------


def now_ms() -> int:
    """当前毫秒时间戳。"""
    return int(time.time() * 1000)


def now_secs() -> int:
    """当前秒级时间戳。"""
    return int(time.time())


def utc_iso() -> str:
    """UTC ISO 串（`%Y-%m-%dT%H:%M:%SZ`）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_date_str(ts_ms: int | None = None) -> str:
    """本地日期 `YYYY-MM-DD`；`ts_ms` 为空取当前时间。

    按本地时区取值：签到「今天」的判定全部走这里，与签到日志的日期字段保持一致。
    """
    if ts_ms is None:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return datetime.now().strftime("%Y-%m-%d")


def parse_clock(raw: str | None) -> tuple[int, int] | None:
    """解析 `"HH:MM"` 本地时钟（允许 1–2 位时/分，如 `"9:5"`）。

    空串、多余字符、越界（`"24:00"` / `"23:60"`）一律返回 `None`。
    """
    if not raw or not isinstance(raw, str):
        return None
    if ":" not in raw:
        return None
    hour_txt, minute_txt = raw.split(":", 1)
    if not (1 <= len(hour_txt) <= 2) or not (1 <= len(minute_txt) <= 2):
        return None
    if not hour_txt.isdigit() or not minute_txt.isdigit():
        return None
    hour, minute = int(hour_txt), int(minute_txt)
    if hour > 23 or minute > 59:
        return None
    return (hour, minute)


def norm_ts(value) -> int | None:
    """把秒/毫秒/字符串时间戳统一为毫秒；无效返回 `None`。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            ts = int(float(value.strip()))
        except (TypeError, ValueError):
            return None
    elif isinstance(value, (int, float)):
        ts = int(value)
    else:
        return None
    if ts < 10_000_000_000:
        ts *= 1000  # 秒 → 毫秒
    return ts


def minute_of_day(ts_ms: int) -> int:
    """`ts_ms` 对应的本地「当日分钟数」（0..1439）。"""
    return datetime.fromtimestamp(ts_ms / 1000).hour * 60 + datetime.fromtimestamp(
        ts_ms / 1000
    ).minute


# ---------------------------------------------------------------------------
# 文件
# ---------------------------------------------------------------------------


def atomic_write(path: str, content: str) -> None:
    """原子写文件（同目录临时文件 + os.replace）。

    服务端常驻进程 + 外部查看场景：直接 write 会在读的瞬间看到半截 JSON，
    原子替换可以避免配置/账号文件被写坏。
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str, default=None):
    """读取 JSON 文件；缺失 / 损坏一律返回 `default`，不抛异常。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path: str, data) -> None:
    """写入 JSON 文件（缩进 2、UTF-8、原子替换）。"""
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def random_hex(n: int) -> str:
    """随机十六进制串（请求头 x-request-id / x-tt-trace-id 用）。"""
    return secrets.token_hex(n // 2 + n % 2)[:n]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def is_route_missing(response: dict) -> bool:
    """响应是否为「该路径不存在」（HTTP 404）。

    billing 路径候选回落**只允许由 404 触发**：401/403 是鉴权问题、10085 是网关
    指纹拦截、`code=-1` 是传输错误，把它们误当成路径问题会掩盖真实原因。
    """
    if not isinstance(response, dict):
        return False

    def _code(value) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return None
        return None

    top = _code(response.get("code"))
    if top is not None:
        return top == 404
    data = response.get("data")
    if isinstance(data, dict):
        nested = _code(data.get("code"))
        return nested == 404
    return False


def http_json(
    url: str,
    method: str = "POST",
    body=None,
    headers: dict | None = None,
    timeout: int = DEFAULT_HTTP_TIMEOUT,
) -> dict:
    """通用 HTTP JSON 请求，返回解析后的 dict（失败也返回 dict，不抛异常）。

    失败时返回 `{"ok": False, "error": …}` 形态的归一结果，语义见模块 docstring。
    """
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(url, data=payload, method=method.upper())
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", DEFAULT_HTTP_USER_AGENT)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        if value is None:
            continue
        request.add_header(key, str(value))

    try:
        with _NO_PROXY_OPENER.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        parsed = _try_parse(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"code": int(exc.code), "message": raw[:500]}
    except Exception as exc:  # 网络异常 / 超时 / DNS / TLS
        reason = str(exc)
        return {"code": -1, "message": reason if reason else exc.__class__.__name__}

    parsed = _try_parse(raw)
    if isinstance(parsed, dict):
        return parsed
    # 2xx 但非 JSON：保留前 500 字符便于排查
    return {"code": -1, "message": f"非 JSON 响应: {raw[:500]}"}


def _try_parse(raw: str):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def seeded_random_hex(n: int) -> str:
    """随机十六进制串（非密用途，日志/占位用）。"""
    return "".join(random.choice("0123456789abcdef") for _ in range(n))
