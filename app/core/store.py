#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持久化存储：数据目录 / 配置 / 账号 / 日志。

盘上布局（数据目录，默认 `~/.qdassistant`，可用 `QD_DATA_DIR` 覆盖）：

    accounts.json          账号（{"workbuddy": [...], "trae": [...]}）
    config.json            全局配置（含两个平台的签到策略）
    checkin_logs.json      签到日志（30 天 / 500 条滚动）
    trae_cooldowns.json    Trae 账号冷却表
    device_map.json        Trae 设备指纹表
    credits_history.json   Trae 积分历史（90 天滚动）
    logs/app.log           运行日志
"""

from __future__ import annotations

import json
import os
import threading
import time

from . import util

# 平台标识：一个账号集合对应一个签到引擎。
PLATFORM_WORKBUDDY = "workbuddy"
PLATFORM_TRAE = "trae"
PLATFORMS = (PLATFORM_WORKBUDDY, PLATFORM_TRAE)

CHECKIN_LOG_KEEP_DAYS = 30
CHECKIN_LOG_MAX_RECORDS = 500
CREDITS_HISTORY_KEEP_DAYS = 90

DEFAULT_CONFIG = {
    # 总开关：关掉后后台调度不再发起任何签到请求。
    "enabled": True,
    # 全局时间段（本地时钟 "HH:MM"）；为空 = 不限制。
    "checkin_start": "",
    "checkin_end": "",
    # 服务端监听
    "host": "0.0.0.0",
    "port": 8790,
    # 访问口令（为空 = 不启用登录）
    "access_password": "",
    # WorkBuddy 平台
    "workbuddy": {
        "enabled": True,
        # token 惰性刷新阈值（小时）：剩余不足即刷新
        "lazy_refresh_hours": 24,
        # 排除自动签到的账号 id
        "excluded_account_ids": [],
    },
    # Trae 平台
    "trae": {
        "enabled": True,
        "excluded_account_ids": [],
        # 多账号之间的串行间隔（秒，0 = 关闭）；防上游频控
        "checkin_gap_secs": 3,
        # 单个账号网络异常重试次数
        "retry": 1,
        # 整轮失败重试轮次
        "round_retries": 2,
    },
    # 后台巡检间隔（分钟）：兜底补签 / 恢复
    "recovery_interval_minutes": 30,
}

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(name: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if name not in _LOCKS:
            _LOCKS[name] = threading.Lock()
        return _LOCKS[name]


# ---------------------------------------------------------------------------
# 数据目录
# ---------------------------------------------------------------------------

_DATA_DIR: str | None = None


def set_data_dir(path: str) -> None:
    """显式指定数据目录（CLI `--data-dir` / fpk `config_init` 注入）。"""
    global _DATA_DIR
    _DATA_DIR = os.path.abspath(path)
    os.makedirs(_DATA_DIR, exist_ok=True)


def data_dir() -> str:
    """数据目录：环境变量 `QD_DATA_DIR` > 显式设置 > `~/.qdassistant`。"""
    global _DATA_DIR
    if _DATA_DIR:
        return _DATA_DIR
    env = os.environ.get("QD_DATA_DIR", "").strip()
    path = env if env else os.path.join(os.path.expanduser("~"), ".qdassistant")
    _DATA_DIR = os.path.abspath(path)
    os.makedirs(_DATA_DIR, exist_ok=True)
    return _DATA_DIR


def path_of(name: str) -> str:
    return os.path.join(data_dir(), name)


def log_dir() -> str:
    return os.path.join(data_dir(), "logs")


def app_log_file() -> str:
    return os.path.join(log_dir(), "app.log")


def app_log(message: str) -> None:
    """追加一行运行日志（失败静默，日志不是主流程）。"""
    try:
        os.makedirs(log_dir(), exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(app_log_file(), "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, override) -> dict:
    """把 `override` 的已知字段覆盖到 `base`（深合并一层）。"""
    if not isinstance(override, dict):
        return base
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict:
    """读取配置；缺失 / 损坏时回落默认值。

    只保留已知字段，避免旧版本残留字段污染新结构。
    """
    raw = util.read_json(path_of("config.json"))
    base = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝，防调用方改写默认值
    if not isinstance(raw, dict):
        return base
    merged = _deep_merge(base, raw)
    # 时间段归一化：合法值零填充，非法值归一为空串（= 不限制）
    for key in ("checkin_start", "checkin_end"):
        clock = util.parse_clock(merged.get(key))
        merged[key] = f"{clock[0]:02d}:{clock[1]:02d}" if clock else ""
    return merged


def save_config(cfg: dict) -> dict:
    """保存配置（只保留已知字段 + 归一化），返回实际落盘的内容。"""
    merged = _deep_merge(json.loads(json.dumps(DEFAULT_CONFIG)), cfg or {})
    for key in ("checkin_start", "checkin_end"):
        clock = util.parse_clock(merged.get(key))
        merged[key] = f"{clock[0]:02d}:{clock[1]:02d}" if clock else ""
    with _lock("config"):
        util.write_json(path_of("config.json"), merged)
    return merged


def platform_config(platform: str, cfg: dict | None = None) -> dict:
    """取某个平台的配置片段（缺失返回 `{}`）。"""
    cfg = cfg if cfg is not None else load_config()
    section = cfg.get(platform)
    return section if isinstance(section, dict) else {}


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------


def load_accounts(platform: str | None = None):
    """读取账号。

    - 传 `platform`：返回该平台账号列表；
    - 不传：返回 `{"workbuddy": [...], "trae": [...]}`。
    """
    raw = util.read_json(path_of("accounts.json"), default={})
    if not isinstance(raw, dict):
        raw = {}
    for name in PLATFORMS:
        if not isinstance(raw.get(name), list):
            raw[name] = []
    if platform:
        return raw.get(platform, [])
    return raw


def save_accounts(data: dict) -> None:
    with _lock("accounts"):
        util.write_json(path_of("accounts.json"), data)


def upsert_account(platform: str, account: dict) -> dict:
    """按 `id` 新增或更新账号（无 id 时自动生成），返回保存后的账号。"""
    if platform not in PLATFORMS:
        raise ValueError(f"未知平台: {platform}")
    account = dict(account)
    if not account.get("id"):
        account["id"] = f"{platform}-{util.now_ms()}-{util.random_hex(6)}"
    with _lock("accounts"):
        data = load_accounts()
        items = data.get(platform, [])
        for index, item in enumerate(items):
            if item.get("id") == account["id"]:
                merged = dict(item)
                merged.update(account)
                items[index] = merged
                break
        else:
            items.append(account)
        data[platform] = items
        util.write_json(path_of("accounts.json"), data)
    return account


def delete_account(platform: str, account_id: str) -> bool:
    """按 id 删除账号；返回是否命中。"""
    with _lock("accounts"):
        data = load_accounts()
        items = data.get(platform, [])
        kept = [item for item in items if item.get("id") != account_id]
        if len(kept) == len(items):
            return False
        data[platform] = kept
        util.write_json(path_of("accounts.json"), data)
    return True


def get_account(platform: str, account_id: str) -> dict | None:
    for item in load_accounts(platform):
        if item.get("id") == account_id:
            return item
    return None


# ---------------------------------------------------------------------------
# 签到日志
# ---------------------------------------------------------------------------


def load_checkin_logs() -> list:
    """读取签到日志（列表，保持插入顺序）。"""
    raw = util.read_json(path_of("checkin_logs.json"), default=[])
    return raw if isinstance(raw, list) else []


def _normalize_logs(logs: list, now_ms_value: int) -> list:
    """30 天过滤 + 保留最近 500 条。"""
    cutoff = now_ms_value - CHECKIN_LOG_KEEP_DAYS * 24 * 3600 * 1000
    kept = [
        entry
        for entry in logs
        if isinstance(entry, dict)
        and isinstance(entry.get("ts"), int)
        and entry["ts"] >= cutoff
    ]
    if len(kept) > CHECKIN_LOG_MAX_RECORDS:
        kept = kept[-CHECKIN_LOG_MAX_RECORDS:]
    return kept


def save_checkin_logs(logs: list) -> None:
    with _lock("checkin_logs"):
        util.write_json(path_of("checkin_logs.json"), _normalize_logs(logs, util.now_ms()))


def add_checkin_log(entry: dict) -> None:
    """追加一条签到日志（读-改-写串行化，避免并发丢条目）。"""
    entry = dict(entry)
    entry.setdefault("ts", util.now_ms())
    entry.setdefault("date", util.local_date_str(entry["ts"]))
    with _lock("checkin_logs"):
        logs = load_checkin_logs()
        logs.append(entry)
        util.write_json(path_of("checkin_logs.json"), _normalize_logs(logs, util.now_ms()))


def compact_checkin_logs() -> bool:
    """启动整理一次历史日志；返回是否有内容变化。"""
    with _lock("checkin_logs"):
        logs = load_checkin_logs()
        normalized = _normalize_logs(logs, util.now_ms())
        if len(normalized) == len(logs):
            return False
        util.write_json(path_of("checkin_logs.json"), normalized)
    return True


def latest_today_result(platform: str, account_id: str) -> str | None:
    """今天该账号最近一次签到结果（`success` / `already` / `error` / ...）。"""
    today = util.local_date_str()
    found = None
    for entry in load_checkin_logs():
        if entry.get("platform") != platform or entry.get("accountId") != account_id:
            continue
        if entry.get("date") != today and util.local_date_str(entry.get("ts")) != today:
            continue
        found = entry.get("result")
    return found


# ---------------------------------------------------------------------------
# Trae 侧写文件
# ---------------------------------------------------------------------------


def load_trae_cooldowns() -> dict:
    raw = util.read_json(path_of("trae_cooldowns.json"), default={})
    if not isinstance(raw, dict):
        return {}
    cooldowns = raw.get("cooldowns")
    return cooldowns if isinstance(cooldowns, dict) else {}


def save_trae_cooldowns(cooldowns: dict) -> None:
    with _lock("trae_cooldowns"):
        util.write_json(
            path_of("trae_cooldowns.json"),
            {"cooldowns": cooldowns, "updated_at": util.utc_iso()},
        )


def load_device_map() -> dict:
    raw = util.read_json(path_of("device_map.json"), default={})
    return raw if isinstance(raw, dict) else {}


def save_device_map(mapping: dict) -> None:
    with _lock("device_map"):
        util.write_json(path_of("device_map.json"), mapping)


def load_credits_history() -> list:
    raw = util.read_json(path_of("credits_history.json"), default=[])
    return raw if isinstance(raw, list) else []


def append_credits_history(records: list) -> None:
    """追加 Trae 积分历史并裁剪到 90 天。"""
    with _lock("credits_history"):
        history = load_credits_history()
        history.extend(records)
        cutoff = util.local_date_str(util.now_ms() - CREDITS_HISTORY_KEEP_DAYS * 24 * 3600 * 1000)
        history = [
            item
            for item in history
            if isinstance(item, dict) and str(item.get("date", "")) >= cutoff
        ]
        util.write_json(path_of("credits_history.json"), history)


def load_checkin_summary() -> dict:
    raw = util.read_json(path_of("checkin_summary.json"), default={})
    return raw if isinstance(raw, dict) else {}


def save_checkin_summary(summary: dict) -> None:
    util.write_json(path_of("checkin_summary.json"), summary)
