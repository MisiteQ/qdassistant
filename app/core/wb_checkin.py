#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 签到引擎。

- 档位策略：签到仅国内版可用（`variant.supports_checkin`）。国际版没有签到接口，
  自动周期、一键签到与单账号签到都在发请求前统一跳过，绝不发起任何请求。
- 请求：`POST <endpoint><billing_path>/checkin-activity-status`（失败回退
  `checkin-status`）查状态；`POST .../daily-checkin` 提交。
- 路径候选**只有 HTTP 404 才回落**下一个（401/10085/传输错误都不是路径问题）。
- 时间段（本地时区）：窗口生效时每个账号每天在窗口内按 (日期, 账号) 确定性抽一个
  目标分钟，只有到点未办的账号才发请求。

对外入口：
    get_checkin_status(account)     查询签到状态
    perform_checkin(account)        直接提交签到
    checkin_account(account)        单账号完整流程（查状态 → 提交 → 写日志）
    run_checkin_all()               批量签到（一键签到 / 账号页刷新并签到）
    run_checkin_cycle()             后台自动轮次
    next_cycle_delay()              下一轮睡眠时长
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import threading

from . import store, util, variant as wb_variant
from .wb_accounts import (
    build_auth_headers,
    display_name,
    ensure_fresh_token,
    envelope_token_error,
    refresh_account_token,
)

# 自动巡检间隔（与 CHECKIN_RECOVERY_INTERVAL 保持一致）
CHECKIN_RECOVERY_INTERVAL = 30 * 60
# 窗口模式下到点失败后的快速重试间隔
FAST_RETRY_INTERVAL = 5 * 60
# 快速重试次数上限（12 × 5 分钟 ≈ 1 小时）
FAST_RETRY_MAX = 12
# 窗口模式下两轮之间的最小间隔（到点即办）
MIN_CYCLE_DELAY = 60

PLATFORM = store.PLATFORM_WORKBUDDY

_CYCLE_LOCK = threading.Lock()
_ACCOUNT_RUNNING: set[str] = set()
_ACCOUNT_LOCK = threading.Lock()

_STATE_LOCK = threading.Lock()
_LAST_CYCLE_HAD_ERRORS = False
_FAST_RETRY_BUDGET = FAST_RETRY_MAX


# ---------------------------------------------------------------------------
# 判定辅助
# ---------------------------------------------------------------------------


def _as_int(value, default=None):
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else default
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def is_unauthorized(resp: dict) -> bool:
    """判断是否因 token 失效被拒（用于触发刷新重试）。"""
    code = _as_int(resp.get("code"), -1)
    if code in (401, 403):
        return True
    message = str(resp.get("message") or resp.get("msg") or "").lower()
    return any(key in message for key in ("unauthorized", "401", "登录", "失效", "过期", "token"))


def is_inactive_message(message: str) -> bool:
    """国际版「功能不可用」类业务提示：签到活动未开启 / 未开放 / 已过期。"""
    raw = (message or "").lower()
    return any(
        key in raw
        for key in ("未开启", "未开放", "已过期", "inactive", "not enabled", "not available")
    )


def _skips_refresh_before_retry(which: str, resp: dict) -> bool:
    """该响应是否应在刷新/重试判定**之前**原样返回。

    国际版「未开启 / 未开放 / 已过期」类业务码是业务结果，不是鉴权失败；而
    `is_unauthorized` 的弱关键字含「过期」，若不先短路就会白刷一次 token。
    """
    if which != wb_variant.AI:
        return False
    return is_inactive_message(str(resp.get("message") or resp.get("msg") or ""))


# ---------------------------------------------------------------------------
# 请求层
# ---------------------------------------------------------------------------


def _checkin_request_once(path: str, account: dict, which: str) -> dict:
    """发单次签到请求；遇到未授权且存在 refresh token 时刷新一次并重试。"""
    error = envelope_token_error(account)
    if error:
        return {"code": -2, "message": error}

    url = f"{wb_variant.api_endpoint(which)}{path}"
    resp = util.http_json(url, "POST", body={}, headers=build_auth_headers(account))
    if _skips_refresh_before_retry(which, resp):
        return resp
    if is_unauthorized(resp) and str(account.get("refresh_token") or "").strip():
        refreshed = refresh_account_token(account)
        resp = util.http_json(url, "POST", body={}, headers=build_auth_headers(refreshed))
    return resp


def checkin_request(suffix: str, account: dict) -> dict:
    """发签到相关请求：路径候选按档位生成，**只有 404 才回落**下一个候选。"""
    which = wb_variant.from_account(account)
    paths = wb_variant.billing_paths(which, f"{wb_variant.CHECKIN_API_PREFIX}{suffix}")
    last = {"code": -1, "message": "无可用签到路径"}
    for index, path in enumerate(paths):
        resp = _checkin_request_once(path, account, which)
        if index + 1 == len(paths) or not util.is_route_missing(resp):
            return resp
        last = resp
    return last


def status_from_response(resp: dict):
    """成功的状态响应 → 结果对象；非成功返回 None。"""
    code = _as_int(resp.get("code"), -1)
    if code not in (0, 200):
        return None
    data = resp.get("data")
    data = data if isinstance(data, dict) else {}
    checked = data.get("today_checked_in")
    if checked is None:
        checked = data.get("todayCheckedIn")
    return {
        "ok": True,
        "todayCheckedIn": bool(checked) if isinstance(checked, bool) else False,
        "raw": data,
    }


def get_checkin_status(account: dict) -> dict:
    """查询签到状态：新接口 `checkin-activity-status`，失败回退 `checkin-status`。

    这两个接口是**国内版专有**；国际版不发起任何请求，直接返回
    `statusUnsupported: True`。
    """
    if wb_variant.from_account(account) != wb_variant.CN:
        return {"ok": False, "statusUnsupported": True, "error": "该档位暂无签到状态接口"}

    resp = checkin_request("/checkin-activity-status", account)
    status = status_from_response(resp)
    if status:
        return status

    resp2 = checkin_request("/checkin-status", account)
    status = status_from_response(resp2)
    if status:
        return status

    code2 = _as_int(resp2.get("code"), -1)
    message = resp2.get("message") or resp2.get("msg")
    return {
        "ok": False,
        "error": str(message) if message else f"code={code2}",
    }


def perform_checkin(account: dict) -> dict:
    """执行签到（POST daily-checkin）；服务端返回已签到提示按成功处理。"""
    which = wb_variant.from_account(account)
    resp = checkin_request("/daily-checkin", account)
    code = _as_int(resp.get("code"), -1)
    if code in (0, 200):
        data = resp.get("data")
        return {"ok": True, "raw": data if isinstance(data, dict) else {}}

    message = str(resp.get("message") or resp.get("msg") or f"code={code}")
    # 幂等业务码（已签到）保持既有 `already` 语义
    if "已签到" in message or "repeat" in message.lower():
        return {"ok": True, "already": True, "message": message}
    # 国际版「功能未开启 / 未开放 / 已过期」= 未开放签到，归类为 `inactive`，
    # 绝不伪造成 success。
    if which == wb_variant.AI and is_inactive_message(message):
        return {"ok": False, "inactive": True, "message": message}
    return {"ok": False, "error": message}


# ---------------------------------------------------------------------------
# 单账号流程
# ---------------------------------------------------------------------------


class _AccountRunGuard:
    """单账号并发保护：同一账号同时只允许一个签到流程。"""

    def __init__(self, key: str):
        self.key = key
        self.acquired = False

    def __enter__(self):
        with _ACCOUNT_LOCK:
            if self.key in _ACCOUNT_RUNNING:
                return self
            _ACCOUNT_RUNNING.add(self.key)
            self.acquired = True
        return self

    def __exit__(self, *_exc):
        if self.acquired:
            with _ACCOUNT_LOCK:
                _ACCOUNT_RUNNING.discard(self.key)
        return False


def account_key(account: dict) -> str:
    """账号标识：稳定 id，缺失时回落展示名。"""
    identifier = account.get("id")
    if isinstance(identifier, str) and identifier:
        return identifier
    return display_name(account)


def _today_answered(account: dict) -> bool:
    """今天是否已有 success / already 日志（防止回执重复刷屏）。"""
    today = util.local_date_str()
    identifier = account.get("id")
    for entry in store.load_checkin_logs():
        if entry.get("platform") != PLATFORM or entry.get("accountId") != identifier:
            continue
        if entry.get("result") not in ("success", "already"):
            continue
        if entry.get("date") == today or util.local_date_str(entry.get("ts")) == today:
            return True
    return False


def _write_today_receipt(account: dict, which: str) -> None:
    """状态查询发现「服务端今日已签」但本地无今日记录 → 补一条 already 回执。

    账号可能已在官方客户端或第三方助手里完成过签到；没有回执时，
    「今日待签到」与账号表「今日」列会一直显示未签，与其他模块的数据对不上。
    """
    if _today_answered(account):
        return
    store.add_checkin_log(
        {
            "ts": util.now_ms(),
            "platform": PLATFORM,
            "accountId": account.get("id"),
            "email": display_name(account),
            "variant": which,
            "result": "already",
        }
    )


def checkin_account(account: dict) -> dict:
    """对单个账号执行完整签到流程：惰性刷新 → 查状态 → 未签到时提交 → 写日志。

    返回 `{"result": success|already|error|skipped|inactive, "error": ...}`。
    """
    guard = _AccountRunGuard(account_key(account))
    with guard:
        if not guard.acquired:
            return {"result": "error", "error": "该账号正在签到，请稍后再试"}

        if not wb_variant.supports_checkin(wb_variant.from_account(account)):
            return {"result": "skipped", "reason": "unsupported_variant"}

        acc = ensure_fresh_token(account)
        which = wb_variant.from_account(acc)
        status = get_checkin_status(acc)

        if status.get("statusUnsupported") is True:
            # 没有状态查询接口的档位允许直接提交一次：daily-checkin 自身幂等，
            # 重复提交会返回「已签到」。
            decision = "submit"
        elif status.get("ok") is not True:
            return {"result": "error", "error": status.get("error") or "查询签到状态失败"}
        elif status.get("todayCheckedIn") is True:
            # 服务端已签但本地没有今天的日志（账号可能已在其他客户端签过）：
            # 补一条 already 回执，否则「今日待签到」「账号表今日列」永远显示未签。
            _write_today_receipt(acc, which)
            return {"result": "already"}
        else:
            decision = "submit"

        if decision != "submit":
            return {"result": "error", "error": "未能判定签到动作"}

        # 只有这个分支真正提交 daily-checkin，因此只有它的结果有资格写签到日志。
        entry = {
            "ts": util.now_ms(),
            "platform": PLATFORM,
            "accountId": acc.get("id"),
            "email": display_name(acc),
            "variant": which,
        }
        result = perform_checkin(acc)
        if result.get("inactive") is True:
            # inactive 既不能记为成功，也不应计为失败 → **不写签到日志**。
            return {
                "result": "inactive",
                "inactive": True,
                "message": result.get("message"),
            }

        if result.get("ok") is True:
            outcome = "already" if result.get("already") else "success"
            error = None
        else:
            outcome = "error"
            error = result.get("error")

        entry["result"] = outcome
        if error:
            entry["error"] = error
        entry["date"] = util.local_date_str(entry["ts"])
        store.add_checkin_log(entry)
        return {"result": outcome, "error": error}


# ---------------------------------------------------------------------------
# 批量 / 调度
# ---------------------------------------------------------------------------


def allows_auto_checkin(account: dict, cfg: dict) -> bool:
    """是否允许该账号参与自动签到（档位支持 + 未被排除）。"""
    if not wb_variant.supports_checkin(wb_variant.from_account(account)):
        return False
    section = store.platform_config(PLATFORM, cfg)
    excluded = section.get("excluded_account_ids") or []
    identifier = account.get("id")
    return not (isinstance(identifier, str) and identifier in excluded)


def auto_checkin_accounts(accounts: list, cfg: dict) -> list:
    return [acc for acc in accounts if allows_auto_checkin(acc, cfg)]


def window_minutes(cfg: dict):
    """窗口生效条件：两个字段都能解析成 `"HH:MM"` 且 `start < end`（不支持跨午夜）。

    返回当日窗口的 `[start_minute, end_minute)`；否则 `None`（= 不限制）。
    """
    start = util.parse_clock(cfg.get("checkin_start"))
    end = util.parse_clock(cfg.get("checkin_end"))
    if not start or not end:
        return None
    start_min = start[0] * 60 + start[1]
    end_min = end[0] * 60 + end[1]
    if start_min < end_min:
        return (start_min, end_min)
    return None


def now_in_window(now_ms_value: int, win: tuple[int, int]) -> bool:
    """`now_ms` 的本地分钟是否落在窗口 `[start, end)` 内。"""
    minute = util.minute_of_day(now_ms_value)
    return win[0] <= minute < win[1]


def _shift_day(day: str, delta: int) -> str | None:
    try:
        date = _dt.date.fromisoformat(day)
    except ValueError:
        return None
    return (date + _dt.timedelta(days=delta)).isoformat()


def target_minute(day: str, key: str, start: int, end: int) -> int:
    """某账号在 `day` 的目标分钟：窗口内按 (本地日期, 账号) 确定性抽签。

    同一天重启不变、不同账号天然错峰。用 SHA-256 保证跨进程结果一致，
    同样满足「确定性 + 均匀分布」。
    """
    digest = hashlib.sha256(f"{day}|{key}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    return start + value % max(1, end - start)


def target_ms_for_day(day: str, account: dict, win: tuple[int, int]) -> int | None:
    """某账号在 `day` 的目标时刻（ms）。"""
    try:
        date = _dt.date.fromisoformat(day)
    except ValueError:
        return None
    minute = target_minute(day, account_key(account), win[0], win[1])
    try:
        moment = _dt.datetime.combine(date, _dt.time(minute // 60, minute % 60))
    except ValueError:
        return None
    return int(moment.timestamp() * 1000)


def latest_target_ms(now_ms_value: int, account: dict, win: tuple[int, int]) -> int | None:
    """最近一个已应到的目标时刻：未到今日目标时取昨日目标。"""
    today = util.local_date_str(now_ms_value)
    today_target = target_ms_for_day(today, account, win)
    if today_target is None:
        return None
    if now_ms_value >= today_target:
        return today_target
    yesterday = _shift_day(today, -1)
    if not yesterday:
        return None
    return target_ms_for_day(yesterday, account, win)


def next_target_ms(now_ms_value: int, account: dict, win: tuple[int, int]) -> int | None:
    """下一个目标时刻（今日未到则今日，已过则明日）。"""
    today = util.local_date_str(now_ms_value)
    today_target = target_ms_for_day(today, account, win)
    if today_target is None:
        return None
    if now_ms_value < today_target:
        return today_target
    tomorrow = _shift_day(today, 1)
    if not tomorrow:
        return None
    return target_ms_for_day(tomorrow, account, win)


def logs_answered_window(logs: list, account: dict, since: int) -> bool:
    """该账号在 `since` 之后是否已有 `success` / `already` 日志。"""
    identifier = account.get("id")
    stable_id = identifier if isinstance(identifier, str) and identifier else None
    name = display_name(account)
    for entry in logs:
        ts = entry.get("ts")
        if not isinstance(ts, int) or ts < since:
            continue
        if entry.get("result") not in ("success", "already"):
            continue
        if stable_id:
            if entry.get("accountId") == stable_id:
                return True
        else:
            entry_id = entry.get("accountId")
            if (not entry_id) and entry.get("email") == name:
                return True
    return False


def account_due(now_ms_value: int, account: dict, win: tuple[int, int], logs: list) -> bool:
    """窗口模式下该账号是否「到点未办」。"""
    latest = latest_target_ms(now_ms_value, account, win)
    if latest is None:
        return True  # 日期无法解释：保守当作到点未办
    return not logs_answered_window(logs, account, latest)


def _write_window_receipt(win: tuple[int, int], account: dict) -> None:
    """窗口模式下到点复核发现服务端「今日已签」→ 补一条 `already` 回执。

    回执让「本窗口已回应」跨重启成立，避免次日 00:0x 被误判为「窗口未回应」而提前签。
    """
    now_ms_value = util.now_ms()
    latest = latest_target_ms(now_ms_value, account, win)
    if latest is None:
        return
    if logs_answered_window(store.load_checkin_logs(), account, latest):
        return
    store.add_checkin_log(
        {
            "ts": now_ms_value,
            "platform": PLATFORM,
            "accountId": account.get("id"),
            "email": display_name(account),
            "variant": wb_variant.from_account(account),
            "result": "already",
        }
    )


def _record_cycle_health(had_errors: bool) -> None:
    global _LAST_CYCLE_HAD_ERRORS, _FAST_RETRY_BUDGET
    with _STATE_LOCK:
        _LAST_CYCLE_HAD_ERRORS = had_errors
        if not had_errors:
            _FAST_RETRY_BUDGET = FAST_RETRY_MAX


def next_cycle_delay() -> int:
    """下一轮睡眠秒数。策略全在本模块，宿主只读这个时长。"""
    global _FAST_RETRY_BUDGET
    cfg = store.load_config()
    win = window_minutes(cfg)
    if win is None:
        return CHECKIN_RECOVERY_INTERVAL

    accounts = auto_checkin_accounts(store.load_accounts(PLATFORM), cfg)
    with _STATE_LOCK:
        had_errors = _LAST_CYCLE_HAD_ERRORS
        budget = _FAST_RETRY_BUDGET
    if had_errors and budget > 0:
        with _STATE_LOCK:
            _FAST_RETRY_BUDGET = budget - 1
        return FAST_RETRY_INTERVAL

    now_ms_value = util.now_ms()
    nearest = None
    for acc in accounts:
        target = next_target_ms(now_ms_value, acc, win)
        if target is None:
            continue
        nearest = target if nearest is None else min(nearest, target)
    if nearest is None:
        return CHECKIN_RECOVERY_INTERVAL
    return max(MIN_CYCLE_DELAY, min(CHECKIN_RECOVERY_INTERVAL, (nearest - now_ms_value) // 1000))


def run_checkin_cycle() -> dict:
    """执行一轮自动签到（后台调度调用）。

    逐账号查询服务端状态；窗口生效时只处理「到点未办」的账号，未到点整轮不发请求。
    """
    if not _CYCLE_LOCK.acquire(blocking=False):
        return {"status": "skipped", "reason": "already_running"}
    try:
        cfg = store.load_config()
        if cfg.get("enabled") is not True:
            return {"status": "disabled"}
        section = store.platform_config(PLATFORM, cfg)
        if section.get("enabled") is not True:
            return {"status": "disabled"}

        candidates = auto_checkin_accounts(store.load_accounts(PLATFORM), cfg)
        if not candidates:
            return {"status": "no_accounts"}

        win = window_minutes(cfg)
        if win is not None:
            logs = store.load_checkin_logs()
            now_ms_value = util.now_ms()
            candidates = [acc for acc in candidates if account_due(now_ms_value, acc, win, logs)]
            if not candidates:
                _record_cycle_health(False)
                return {"status": "skipped", "reason": "before_checkin_time", "accounts": []}

        rows = []
        had_errors = False
        for acc in candidates:
            result = checkin_account(acc)
            outcome = result.get("result")
            if outcome == "error":
                had_errors = True
            if outcome == "already" and win is not None:
                _write_window_receipt(win, acc)
            row = {
                "email": display_name(acc),
                "accountId": acc.get("id"),
                "result": outcome,
                "error": result.get("error"),
            }
            if result.get("inactive"):
                row["inactive"] = True
            rows.append(row)
        _record_cycle_health(had_errors)
        return {"status": "ok", "accounts": rows}
    finally:
        _CYCLE_LOCK.release()


def run_checkin_all(respect_window: bool = False) -> dict:
    """对全部 WorkBuddy 账号批量签到（一键签到）。

    `respect_window=True` 时：窗口生效且当前不在 `[start, end)` 内则整轮跳过，
    逐账号返回 `skipped / outside_checkin_window`，窗口外不发任何签到请求。
    """
    if not _CYCLE_LOCK.acquire(blocking=False):
        return {"status": "skipped", "reason": "already_running"}
    try:
        cfg = store.load_config()
        accounts = store.load_accounts(PLATFORM)
        if not accounts:
            return {"status": "no_accounts", "accounts": []}

        win = window_minutes(cfg)
        if respect_window and win is not None and not now_in_window(util.now_ms(), win):
            rows = [
                {
                    "email": display_name(acc),
                    "accountId": acc.get("id"),
                    "result": "skipped",
                    "reason": "outside_checkin_window",
                }
                for acc in accounts
            ]
            return {"status": "skipped", "reason": "outside_checkin_window", "accounts": rows}

        rows = []
        for acc in accounts:
            if not allows_auto_checkin(acc, cfg):
                rows.append(
                    {
                        "email": display_name(acc),
                        "accountId": acc.get("id"),
                        "result": "skipped",
                        "reason": "auto_checkin_disabled",
                    }
                )
                continue
            result = checkin_account(acc)
            row = {
                "email": display_name(acc),
                "accountId": acc.get("id"),
                "result": result.get("result"),
                "error": result.get("error"),
            }
            if result.get("inactive"):
                row["inactive"] = True
            rows.append(row)
        return {"status": "ok", "accounts": rows}
    finally:
        _CYCLE_LOCK.release()


def accounts_checked_in_today() -> bool:
    """今天是否所有应签到的 WorkBuddy 账号都已签到。

    账号库为空返回 False（保留「一键签到」入口）；没有任何账号需要签到时返回 True。
    """
    cfg = store.load_config()
    accounts = store.load_accounts(PLATFORM)
    if not accounts:
        return False
    pending = [acc for acc in accounts if allows_auto_checkin(acc, cfg)]
    if not pending:
        return True
    today = util.local_date_str()
    return all(store.latest_today_result(PLATFORM, acc.get("id", "")) in ("success", "already") for acc in pending)
