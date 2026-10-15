#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台调度循环。

服务启动后自动跑起来，无需人工干预——这正是搬到 NAS 上 7×24 常驻的意义所在。

两个平台各一条独立线程：
- WorkBuddy：启动即核验一轮，之后按 `wb_checkin.next_cycle_delay()` 睡眠；
- Trae：启动即核验一轮，之后按巡检间隔睡眠；
- 另有一条线程按日整理历史日志（30 天滚动）。

线程都是 daemon，主进程退出即结束；单轮异常不会终止循环。
"""

from __future__ import annotations

import threading
import time

from . import store, util
from . import trae_checkin, wb_checkin

# 启动后延迟几秒再跑第一轮：避开服务刚起来时的资源竞争
STARTUP_DELAY_SECONDS = 5
# 日志整理周期（秒）
COMPACT_INTERVAL = 6 * 3600

_started = False
_start_lock = threading.Lock()
_threads: list[threading.Thread] = []

_last_runs: dict[str, dict] = {}
_last_runs_lock = threading.Lock()


def _remember(platform: str, payload: dict) -> None:
    with _last_runs_lock:
        payload = dict(payload)
        payload["_at"] = util.now_ms()
        _last_runs[platform] = payload


def last_runs() -> dict:
    """最近一轮调度结果（供前端「调度状态」展示）。"""
    with _last_runs_lock:
        return {key: dict(value) for key, value in _last_runs.items()}


def _recovery_interval_seconds(cfg: dict) -> int:
    minutes = cfg.get("recovery_interval_minutes")
    if isinstance(minutes, (int, float)) and minutes > 0:
        return int(minutes) * 60
    return wb_checkin.CHECKIN_RECOVERY_INTERVAL


def _workbuddy_loop() -> None:
    time.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            result = wb_checkin.run_checkin_cycle()
            _remember(store.PLATFORM_WORKBUDDY, result)
            if result.get("status") == "ok":
                store.app_log("[调度] WorkBuddy 签到轮次完成")
            elif result.get("status") not in ("skipped",):
                store.app_log(f"[调度] WorkBuddy 签到轮次: {result.get('status')}")
        except Exception as exc:  # 单轮异常不终止循环
            store.app_log(f"[调度] WorkBuddy 签到异常: {exc}")
            _remember(store.PLATFORM_WORKBUDDY, {"status": "error", "error": str(exc)})
        try:
            delay = wb_checkin.next_cycle_delay()
        except Exception:
            delay = wb_checkin.CHECKIN_RECOVERY_INTERVAL
        time.sleep(max(30, delay))


def _trae_loop() -> None:
    time.sleep(STARTUP_DELAY_SECONDS + 2)
    while True:
        try:
            result = trae_checkin.run_checkin_cycle()
            _remember(store.PLATFORM_TRAE, result)
            if result.get("status") == "ok":
                store.app_log(
                    f"[调度] Trae 签到轮次完成: 成功 {result.get('ok')} · "
                    f"已签 {result.get('already')} · 失败 {result.get('failed')}"
                )
            elif result.get("status") not in ("skipped",):
                store.app_log(f"[调度] Trae 签到轮次: {result.get('status')}")
        except Exception as exc:
            store.app_log(f"[调度] Trae 签到异常: {exc}")
            _remember(store.PLATFORM_TRAE, {"status": "error", "error": str(exc)})
        cfg = store.load_config()
        time.sleep(max(60, _recovery_interval_seconds(cfg)))


def _compact_loop() -> None:
    while True:
        time.sleep(COMPACT_INTERVAL)
        try:
            if store.compact_checkin_logs():
                store.app_log("[调度] 签到日志已按 30 天滚动整理")
        except Exception as exc:
            store.app_log(f"[调度] 日志整理失败: {exc}")


def start() -> None:
    """启动后台调度线程（幂等：重复调用不会起第二条）。"""
    global _started
    with _start_lock:
        if _started:
            return
        try:
            store.compact_checkin_logs()
        except Exception as exc:
            store.app_log(f"[调度] 启动日志整理失败: {exc}")

        for name, target in (
            ("qd-workbuddy", _workbuddy_loop),
            ("qd-trae", _trae_loop),
            ("qd-compact", _compact_loop),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            _threads.append(thread)
        _started = True
        store.app_log("[调度] 后台签到循环已启动")
