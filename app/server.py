#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QDAssistant 签到助手 —— 服务端入口。

一个常驻 HTTP 服务，自带 Web 界面与后台签到调度，
无需桌面端即可 7×24 运行（飞牛 NAS 场景）。

用法：
    python3 server.py                       # 默认 0.0.0.0:8790 + 启动浏览器
    python3 server.py --port 9000           # 指定端口
    python3 server.py --no-open             # 不开浏览器
    python3 server.py --data-dir /vol1/@appdata/qdassistant

设计约束：
- 零第三方依赖（仅 Python 标准库），NAS 上开箱即用；
- 监听 `0.0.0.0`（默认），局域网内任意设备可访问；
- 状态变更接口需带 `X-QD-Request: 1` 头，阻断浏览器跨站表单提交（CSRF）。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import (  # noqa: E402
    APP_AUTHOR,
    APP_AUTHOR_URL,
    APP_NAME,
    APP_REPO_URL,
    APP_VERSION,
    oauth,
    scheduler,
    store,
    trae_checkin,
    trae_identity,
    trae_stats,
    trae_storage,
    util,
    wb_accounts,
    wb_checkin,
    wb_stats,
)

WWW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
DEFAULT_PORT = 8790

# 内存会话：access_password 为空时不启用登录
_SESSIONS: set[str] = set()
_SESSION_LOCK = threading.Lock()


class ApiError(Exception):
    """业务错误：带 HTTP 状态码，供客户端区分「参数错」与「服务错」。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status

# 异步签到任务（批量签到可能耗时较长，避免阻塞 HTTP 响应）
_TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()
# 进程启动时刻：接口只读，用于「关于」里展示运行时长
_STARTED_AT = time.time()
TASK_KEEP_SECONDS = 3600


# ---------------------------------------------------------------------------
# 鉴权 / CSRF
# ---------------------------------------------------------------------------


def _password_enabled() -> bool:
    return bool(str(store.load_config().get("access_password") or "").strip())


def _issue_session() -> str:
    token = secrets.token_urlsafe(32)
    with _SESSION_LOCK:
        _SESSIONS.add(token)
    return token


def _valid_session(token: str | None) -> bool:
    if not token:
        return False
    with _SESSION_LOCK:
        return token in _SESSIONS


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    """登录态校验：未设置口令时一律放行。"""
    if not _password_enabled():
        return True
    cookie = handler.headers.get("Cookie") or ""
    for part in cookie.split(";"):
        key, _, value = part.strip().partition("=")
        if key == "qd_token" and _valid_session(value):
            return True
    return False


def _csrf_ok(handler: BaseHTTPRequestHandler) -> bool:
    """CSRF 闸：状态变更请求必须带 `X-QD-Request: 1`。

    浏览器跨站提交无法自定义请求头（会先走 CORS 预检，而本服务不放行），
    因此这一道闸足以阻断跨站表单。
    """
    return handler.headers.get("X-QD-Request") == "1"


# ---------------------------------------------------------------------------
# 任务管理
# ---------------------------------------------------------------------------


def _new_task(platform: str) -> str:
    task_id = f"{platform}-{util.now_ms()}-{secrets.token_hex(4)}"
    with _TASKS_LOCK:
        _TASKS[task_id] = {
            "id": task_id,
            "platform": platform,
            "status": "running",
            "rows": [],
            "startedAt": util.now_ms(),
            "finishedAt": None,
            "result": None,
        }
    return task_id


def _finish_task(task_id: str, result: dict) -> None:
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return
        task["status"] = "done"
        task["finishedAt"] = util.now_ms()
        task["result"] = result
        task["rows"] = result.get("accounts") or result.get("rows") or []
    # 过期任务清理
    with _TASKS_LOCK:
        cutoff = util.now_ms() - TASK_KEEP_SECONDS * 1000
        for key in [k for k, v in _TASKS.items() if (v.get("startedAt") or 0) < cutoff]:
            _TASKS.pop(key, None)


def _run_workbuddy_task(task_id: str, account_id: str | None) -> None:
    try:
        if account_id:
            account = store.get_account(store.PLATFORM_WORKBUDDY, account_id)
            if not account:
                result = {"status": "not_found"}
            else:
                outcome = wb_checkin.checkin_account(account)
                result = {"status": "ok", "accounts": [dict(outcome, accountId=account_id)]}
        else:
            result = wb_checkin.run_checkin_all()
    except Exception as exc:
        result = {"status": "error", "error": str(exc)}
    _finish_task(task_id, result)


def _run_trae_task(task_id: str, account_id: str | None) -> None:
    try:
        if account_id:
            account = store.get_account(store.PLATFORM_TRAE, account_id)
            if not account:
                result = {"status": "not_found"}
            else:
                cfg = store.load_config()
                section = store.platform_config(store.PLATFORM_TRAE, cfg)
                outcome = trae_checkin.run_round(
                    [account],
                    retry=int(section.get("retry") or 1),
                    gap_secs=0,
                )
                result = {"status": "ok", "rows": outcome["rows"]}
        else:
            result = trae_checkin.run_checkin_cycle()
    except Exception as exc:
        result = {"status": "error", "error": str(exc)}
    _finish_task(task_id, result)


# ---------------------------------------------------------------------------
# 账号视图
# ---------------------------------------------------------------------------


def _mask(value: str) -> str:
    """凭据脱敏：只保留首尾各 4 位，中间打码。"""
    if not isinstance(value, str):
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


def workbuddy_view(account: dict) -> dict:
    which = "cn"
    try:
        from core import variant as wb_variant

        which = wb_variant.from_account(account)
    except Exception:
        pass
    expires_at = util.norm_ts(account.get("expiresAt"))
    return {
        "id": account.get("id"),
        "platform": store.PLATFORM_WORKBUDDY,
        "name": wb_accounts.display_name(account),
        "variant": which,
        "uid": account.get("uid"),
        "email": account.get("email"),
        "nickname": account.get("nickname"),
        "domain": account.get("domain"),
        "accessTokenMasked": _mask(account.get("access_token") or ""),
        "hasRefreshToken": bool(str(account.get("refresh_token") or "").strip()),
        "expiresAt": expires_at,
        "needsRelogin": bool(account.get("needs_relogin")),
        "needsReloginReason": account.get("needs_relogin_reason"),
        "supportsCheckin": wb_checkin_allows(account),
        "todayResult": store.latest_today_result(store.PLATFORM_WORKBUDDY, account.get("id") or ""),
    }


def wb_checkin_allows(account: dict) -> bool:
    from core import variant as wb_variant

    return wb_variant.supports_checkin(wb_variant.from_account(account))


def trae_view(account: dict) -> dict:
    jwt = str(account.get("jwt") or "")
    info = trae_identity.jwt_parse(jwt)
    uid = account.get("user_id") or info["user_id"] or ""
    cooldown = trae_checkin.cooldown_active(uid) if uid else None
    return {
        "id": account.get("id"),
        "platform": store.PLATFORM_TRAE,
        "name": account.get("name") or uid or "未命名账号",
        "userId": uid,
        "email": account.get("email") or info.get("email") or "",
        "jwtMasked": _mask(jwt),
        "jwtStatus": trae_identity.jwt_status(info["exp_hours"]),
        "expHours": round(info["exp_hours"], 1) if info["exp_hours"] is not None else None,
        "expTimestamp": info["exp_timestamp"],
        "hasRefreshToken": bool(str(account.get("refresh_token") or "").strip()),
        "refreshError": account.get("refresh_error"),
        "cooldown": cooldown,
        "cooldownUntil": trae_checkin.cooldown_until(uid) if uid else 0,
        "todayResult": store.latest_today_result(store.PLATFORM_TRAE, uid),
    }


def _view(platform: str, account: dict) -> dict:
    return trae_view(account) if platform == store.PLATFORM_TRAE else workbuddy_view(account)


# ---------------------------------------------------------------------------
# 路由实现
# ---------------------------------------------------------------------------


def api_status(_query: dict, _body) -> dict:
    cfg = store.load_config()
    accounts = store.load_accounts()
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "time": util.now_ms(),
        "localDate": util.local_date_str(),
        "dataDir": store.data_dir(),
        "autoEnabled": cfg.get("enabled") is True,
        "workbuddyEnabled": cfg.get("enabled") is True and (cfg.get("workbuddy") or {}).get("enabled") is not False,
        "traeEnabled": cfg.get("enabled") is True and (cfg.get("trae") or {}).get("enabled") is not False,
        "authRequired": _password_enabled(),
        "counts": {key: len(value) for key, value in accounts.items()},
        "scheduler": scheduler.last_runs(),
        "uptimeSeconds": int(time.time() - _STARTED_AT),
    }


def api_accounts(query: dict, _body) -> dict:
    platform = (query.get("platform") or [""])[0]
    if platform in store.PLATFORMS:
        accounts = store.load_accounts(platform)
        return {"accounts": [_view(platform, acc) for acc in accounts]}
    data = store.load_accounts()
    return {
        "accounts": [
            _view(name, acc) for name in store.PLATFORMS for acc in data.get(name, [])
        ]
    }


def api_account_add(query: dict, body: dict) -> dict:
    platform = body.get("platform") or (query.get("platform") or [""])[0]
    if platform not in store.PLATFORMS:
        raise ApiError(f"未知平台: {platform}")

    if platform == store.PLATFORM_TRAE:
        jwt = str(body.get("jwt") or "").strip()
        if not jwt:
            raise ApiError("请填写 Trae JWT")
        info = trae_identity.jwt_parse(jwt)
        if not info["user_id"]:
            raise ApiError("无法从该 JWT 解析出 user id，请确认粘贴完整")
        # 名称优先级：用户填写 > JWT 里的昵称 > 邮箱 > user id
        display_name = (
            str(body.get("name") or "").strip()
            or info.get("name")
            or info.get("email")
            or info["user_id"]
        )
        account = {
            "name": display_name,
            "jwt": jwt,
            "user_id": info["user_id"],
        }
        if info.get("email"):
            account["email"] = info["email"]
        # 同一 user_id 视为同一账号，更新而不是重复添加
        for existing in store.load_accounts(platform):
            if existing.get("user_id") == info["user_id"]:
                account["id"] = existing.get("id")
                break
    else:
        token = str(body.get("access_token") or "").strip()
        if not token:
            raise ApiError("请填写 access_token")
        account = {
            "access_token": token,
            "refresh_token": str(body.get("refresh_token") or "").strip(),
            "uid": body.get("uid"),
            "nickname": body.get("nickname"),
            "email": body.get("email"),
            "domain": body.get("domain"),
            "variant": body.get("variant") or "cn",
        }
        if body.get("expiresAt") is not None:
            account["expiresAt"] = util.norm_ts(body.get("expiresAt"))

    saved = store.upsert_account(platform, account)
    return {"ok": True, "account": _view(platform, saved)}


def api_account_import_storage(_query: dict, body: dict) -> dict:
    """导入 Trae 客户端本机凭据文件（全局存储）免 OAuth 直接入库。

    Trae 的 OAuth 把凭证推给浏览器所在机器的客户端，且 AuthCode 交换要校验设备
    私钥签名——NAS 上没有客户端，那条链路走不通；而客户端登录后会把凭据写在本机，
    用户把文件内容贴进来即可解出 JWT（带 refresh_token 时还能自动续期）。
    内容只在服务端内存中解析，**不写任何文件、不进日志**。
    """
    content = str(body.get("content") or "")
    try:
        creds = trae_storage.credentials_from_storage(content)
    except ValueError as exc:
        raise ApiError(str(exc))

    jwt = creds["jwt"]
    info = trae_identity.jwt_parse(jwt)
    if not info["user_id"]:
        raise ApiError("解出的凭据里没有 user id——请确认客户端已登录且凭据未过期")

    # 名称优先级：用户填写的备注 > 凭据自带昵称 > JWT 里的昵称/邮箱 > user id
    display_name = (
        str(body.get("name") or "").strip()
        or creds.get("userName")
        or info.get("name")
        or creds.get("email")
        or info.get("email")
        or info["user_id"]
    )
    account = {
        "jwt": jwt,
        "user_id": info["user_id"],
        "name": display_name,
    }
    email = creds.get("email") or info.get("email")
    if email:
        account["email"] = email
    if creds.get("refreshToken"):
        account["refresh_token"] = creds["refreshToken"]
    # 同 user_id 视为同一账号：更新而不是重复添加。
    # 旧名称若还是默认的 user id 形态（或为空），这次有了更可读的名字就用新的；
    # 用户自定义过的名字永远保留。
    for existing in store.load_accounts(store.PLATFORM_TRAE):
        if existing.get("user_id") == info["user_id"]:
            account["id"] = existing.get("id")
            old_name = str(existing.get("name") or "").strip()
            if old_name and old_name != str(existing.get("user_id") or ""):
                account["name"] = old_name
            break

    saved = store.upsert_account(store.PLATFORM_TRAE, account)
    return {
        "ok": True,
        "account": _view(store.PLATFORM_TRAE, saved),
        "host": creds.get("host") or "",
        "hasRefreshToken": bool(creds.get("refreshToken")),
        "identity": {"name": display_name, "email": email or "", "userId": info["user_id"]},
    }


def api_account_delete(_query: dict, body: dict) -> dict:
    platform = body.get("platform")
    account_id = body.get("id")
    if platform not in store.PLATFORMS or not account_id:
        raise ApiError("缺少 platform 或 id")
    return {"ok": store.delete_account(platform, account_id)}


def api_account_refresh(_query: dict, body: dict) -> dict:
    """手动刷新 WorkBuddy token。"""
    account_id = body.get("id")
    account = store.get_account(store.PLATFORM_WORKBUDDY, account_id or "")
    if not account:
        raise ApiError("账号不存在", 404)
    refreshed = wb_accounts.refresh_account_token(account)
    return {"ok": True, "account": workbuddy_view(refreshed)}


def api_account_refresh_jwt(_query: dict, body: dict) -> dict:
    """手动刷新 Trae JWT（refresh_token 换新 token）。"""
    account_id = body.get("id")
    account = store.get_account(store.PLATFORM_TRAE, account_id or "")
    if not account:
        raise ApiError("账号不存在", 404)
    if not str(account.get("refresh_token") or "").strip():
        raise ApiError("该账号没有 refresh_token（手动粘贴添加的账号无法自动续期），请重新粘贴新 JWT")
    refreshed = oauth.ensure_fresh_trae_jwt(account)
    if refreshed.get("refresh_error"):
        raise ApiError(f"刷新失败: {refreshed['refresh_error']}")
    return {"ok": True, "account": trae_view(refreshed)}


# ---------------------------------------------------------------------------
# OAuth 登录
# ---------------------------------------------------------------------------


def api_oauth_wb_start(_query: dict, body: dict) -> dict:
    """发起 WorkBuddy OAuth：返回授权链接，前端引导用户打开。"""
    result = oauth.wb_oauth_start(body.get("variant") or "cn")
    return {"ok": True, **result}


def api_oauth_wb_poll(_query: dict, body: dict) -> dict:
    """轮询 WorkBuddy OAuth 结果；`done=true` 时账号已入库。"""
    return oauth.wb_oauth_poll(body.get("loginId") or "")


def api_oauth_trae_start(_query: dict, _body: dict) -> dict:
    """生成 Trae OAuth 登录链接（授权后需复制回调 URL 粘贴回来）。"""
    result = oauth.trae_oauth_start()
    return {"ok": True, **result}


def api_oauth_trae_callback_check(_query: dict, body: dict) -> dict:
    """粘贴内容即时校验（纯本地判定，不发起任何外部请求）。

    返回 `{"ok", "kind", "reason", "expired", "remainingSeconds"}`，前端据此在
    提交前就提示「粘错了 / 已过期」，而不是等一次必然失败的交换。
    """
    callback_url = str(body.get("callbackUrl") or "").strip()
    if not callback_url:
        raise ApiError("请粘贴授权完成后的完整回调 URL")
    return {"ok": True, "check": oauth.trae_callback_precheck(callback_url)}


def api_oauth_trae_callback(_query: dict, body: dict) -> dict:
    """提交粘贴的 Trae OAuth 回调 URL，完成交换并入库。"""
    callback_url = str(body.get("callbackUrl") or "").strip()
    if not callback_url:
        raise ApiError("请粘贴授权完成后的完整回调 URL")
    try:
        result = oauth.trae_oauth_callback(callback_url)
    except oauth.OAuthExpiredError as exc:
        # 过期不算错误码路径：以 200 + expired 标记回给前端，界面直接提示重来
        return {"ok": False, "expired": True, "error": str(exc)}
    return {"ok": True, **result}


def _as_int(value, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def api_stats_wb_credits(query: dict, _body) -> dict:
    """全账号积分余额与积分包（计费域查询，10 分钟缓存；force=1 绕过）。"""
    force = (query.get("force") or ["0"])[0] == "1"
    return wb_stats.fetch_all_credits(force)


def api_stats_wb_usage(query: dict, _body) -> dict:
    """全账号官方请求用量（近 N 天，默认 31）。"""
    days = _as_int((query.get("days") or ["31"])[0], 31)
    days = max(1, min(days, 90))
    force = (query.get("force") or ["0"])[0] == "1"
    return wb_stats.fetch_all_usage(days, force)


def api_stats_trae_usage(query: dict, _body) -> dict:
    """全账号 Trae 按会话用量（近 N 天，默认 30）。"""
    days = _as_int((query.get("days") or ["30"])[0], 30)
    days = max(1, min(days, 90))
    force = (query.get("force") or ["0"])[0] == "1"
    return trae_stats.fetch_all_usage(days, force)


def api_stats_trae_credits(query: dict, _body) -> dict:
    """全账号 Trae 积分包与剩余积分（与 WorkBuddy 侧同构输出）。"""
    force = (query.get("force") or ["0"])[0] == "1"
    return trae_stats.fetch_all_credits(force)


def api_checkin_start(query: dict, body: dict) -> dict:
    """启动一次签到（异步），返回 `taskId` 供前端轮询进度。"""
    platform = body.get("platform") or (query.get("platform") or [""])[0]
    if platform not in store.PLATFORMS:
        raise ApiError(f"未知平台: {platform}")
    account_id = body.get("id")
    task_id = _new_task(platform)
    target = _run_trae_task if platform == store.PLATFORM_TRAE else _run_workbuddy_task
    threading.Thread(target=target, args=(task_id, account_id), daemon=True).start()
    return {"ok": True, "taskId": task_id}


def api_checkin_progress(query: dict, _body) -> dict:
    task_id = (query.get("taskId") or [""])[0]
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
    if not task:
        raise ApiError("任务不存在或已过期", 404)
    return dict(task)


def api_checkin_status(query: dict, _body) -> dict:
    """一键签到前的状态概览：今天各平台待签到情况。"""
    platform = (query.get("platform") or [""])[0]
    result = {}
    if platform in ("", store.PLATFORM_WORKBUDDY):
        accounts = store.load_accounts(store.PLATFORM_WORKBUDDY)
        result["workbuddy"] = {
            "total": len(accounts),
            "pending": [
                acc.get("id")
                for acc in accounts
                if store.latest_today_result(store.PLATFORM_WORKBUDDY, acc.get("id") or "")
                not in ("success", "already")
            ],
        }
    if platform in ("", store.PLATFORM_TRAE):
        accounts = store.load_accounts(store.PLATFORM_TRAE)
        pending = []
        for acc in accounts:
            uid = trae_checkin.account_uid(acc)
            if store.latest_today_result(store.PLATFORM_TRAE, uid) in ("success", "already"):
                continue
            pending.append(acc.get("id"))
        result["trae"] = {"total": len(accounts), "pending": pending}
    return result


def api_checkin_logs(query: dict, _body) -> dict:
    limit_raw = (query.get("limit") or ["200"])[0]
    try:
        limit = max(1, min(1000, int(limit_raw)))
    except ValueError:
        limit = 200
    platform = (query.get("platform") or [""])[0]
    logs = store.load_checkin_logs()
    if platform in store.PLATFORMS:
        logs = [entry for entry in logs if entry.get("platform") == platform]
    logs = list(reversed(logs))[:limit]
    return {"logs": logs}


def api_config_get(_query: dict, _body) -> dict:
    cfg = store.load_config()
    # 口令不回传明文，只告诉前端是否已设置
    safe = dict(cfg)
    safe["access_password_set"] = bool(str(cfg.get("access_password") or "").strip())
    safe["access_password"] = ""
    return {"config": safe}


def api_config_save(_query: dict, body: dict) -> dict:
    incoming = dict(body or {})
    # 空口令表示「不改」；显式 `access_password: ""` 且带 `clear_password` 才清空
    if not str(incoming.get("access_password") or "").strip():
        incoming.pop("access_password", None)
        if incoming.pop("clear_password", False):
            incoming["access_password"] = ""
    saved = store.save_config(incoming)
    saved["access_password_set"] = bool(str(saved.get("access_password") or "").strip())
    saved["access_password"] = ""
    return {"ok": True, "config": saved}


def api_login(_query: dict, body: dict) -> dict:
    expected = str(store.load_config().get("access_password") or "").strip()
    if not expected:
        return {"ok": True}
    provided = str(body.get("password") or "")
    if not secrets.compare_digest(provided, expected):
        raise ApiError("口令不正确", 401)
    token = _issue_session()
    return {"ok": True, "token": token}


def api_logout(_query: dict, _body) -> dict:
    return {"ok": True}


def api_app_logs(query: dict, _body) -> dict:
    """返回运行日志尾部（默认 200 行）。"""
    try:
        limit = max(1, min(2000, int((query.get("limit") or ["200"])[0])))
    except ValueError:
        limit = 200
    path = store.app_log_file()
    if not os.path.exists(path):
        return {"lines": []}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return {"lines": []}
    return {"lines": lines[-limit:]}


# ---------------------------------------------------------------------------
# 配置导出 / 导入（账号搬家）
# ---------------------------------------------------------------------------

EXPORT_SCHEMA = "qdassistant.config/v1"
# 换设备必须重设的机器相关字段：带过去只会造成串号 / 端口冲突
_EXPORT_SKIP_CONFIG_KEYS = ("host", "port", "access_password")
# 账号里的本机运行时字段：不是账号凭据，搬家时不参与
_EXPORT_SKIP_ACCOUNT_KEYS = ("todayResult", "lastCheckinDate", "cooldownUntil", "lastError")


def _export_accounts() -> dict:
    data = store.load_accounts()
    out: dict[str, list] = {}
    for platform in store.PLATFORMS:
        out[platform] = [
            {key: value for key, value in acc.items() if key not in _EXPORT_SKIP_ACCOUNT_KEYS}
            for acc in data.get(platform, [])
            if isinstance(acc, dict)
        ]
    return out


def api_export_config(_query: dict, _body) -> dict:
    """导出全部账号与签到策略，供另一台设备导入（账号搬家）。

    不含监听地址 / 端口 / 访问口令 —— 这三项属于单机属性，导入到新设备
    只会造成端口冲突或把人锁在旧口令里。
    """
    cfg = store.load_config()
    accounts = _export_accounts()
    return {
        "schema": EXPORT_SCHEMA,
        "app": APP_NAME,
        "version": APP_VERSION,
        "exportedAt": util.utc_iso(),
        "counts": {name: len(accounts.get(name, [])) for name in store.PLATFORMS},
        "policy": {key: value for key, value in cfg.items() if key not in _EXPORT_SKIP_CONFIG_KEYS},
        "accounts": accounts,
    }


def _identity_keys(platform: str, acc: dict) -> list[str]:
    """账号的候选身份键：跨设备稳定，不依赖本机生成的 id。

    WorkBuddy 优先 uid / 邮箱，Trae 优先 user id；两者都缺时退回到 id ——
    导出文件保留 id，所以「导出→导入→再导入」能命中同一条记录，不会翻倍。
    """
    if platform == store.PLATFORM_TRAE:
        raw = (acc.get("user_id"), acc.get("id"))
    else:
        raw = (acc.get("uid"), acc.get("email"), acc.get("id"))
    return [str(item).strip() for item in raw if str(item or "").strip()]


def _lookup_account(index: dict, keys: list[str]) -> dict | None:
    for key in keys:
        if key in index:
            return index[key]
    return None


def api_import_config(_query: dict, body: dict) -> dict:
    """导入导出文件：账号按身份去重合并，签到策略可选应用。

    同名账号（WorkBuddy 按 uid / 邮箱，Trae 按 user id）就地更新而不是重复添加；
    本机 id 与用户自定义备注始终保留，避免搬家把本机信息冲掉。
    """
    doc = body.get("document") if isinstance(body.get("document"), dict) else body
    if not isinstance(doc, dict) or not isinstance(doc.get("accounts"), dict):
        raise ApiError("文件里没有 accounts 段 —— 请选择由本应用「配置导出」生成的文件")

    incoming = doc["accounts"]
    data = store.load_accounts()
    added: dict[str, int] = {}
    updated: dict[str, int] = {}
    skipped: dict[str, int] = {}

    for platform in store.PLATFORMS:
        rows = incoming.get(platform)
        rows = rows if isinstance(rows, list) else []
        items = list(data.get(platform, []))
        index: dict[str, dict] = {}
        for item in items:
            for key in _identity_keys(platform, item):
                index.setdefault(key, item)
        add_count = 0
        upd_count = 0
        skip_count = 0
        for row in rows:
            if not isinstance(row, dict):
                skip_count += 1
                continue
            account = {
                key: value
                for key, value in row.items()
                if key not in _EXPORT_SKIP_ACCOUNT_KEYS
            }
            if not account or not (account.get("access_token") or account.get("jwt")):
                skip_count += 1
                continue
            keys = _identity_keys(platform, account)
            target = _lookup_account(index, keys)
            if target is not None:
                local_name = str(target.get("name") or "").strip()
                local_id = target.get("id")
                merged = dict(target)
                merged.update(account)
                if local_name and local_name != str(target.get("user_id") or ""):
                    merged["name"] = local_name  # 本机备注优先
                if local_id:
                    merged["id"] = local_id
                for position, item in enumerate(items):
                    if item is target:
                        items[position] = merged
                        break
                for key in keys:
                    index[key] = merged
                upd_count += 1
            else:
                # 保留导出文件里的 id：换机后再次导入同一份文件仍能命中同一条记录
                if not str(account.get("id") or "").strip():
                    account["id"] = f"{platform}-{util.now_ms()}-{util.random_hex(6)}"
                if not str(account.get("name") or "").strip():
                    account["name"] = str(
                        account.get("nickname")
                        or account.get("user_id")
                        or account.get("uid")
                        or "未命名账号"
                    )
                items.append(account)
                for key in _identity_keys(platform, account):
                    index.setdefault(key, account)
                add_count += 1
        data[platform] = items
        added[platform] = add_count
        updated[platform] = upd_count
        if skip_count:
            skipped[platform] = skip_count

    store.save_accounts(data)

    policy_applied = False
    if body.get("applyPolicy") is True and isinstance(doc.get("policy"), dict):
        store.save_config(doc["policy"])
        policy_applied = True

    return {
        "ok": True,
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "policyApplied": policy_applied,
        "counts": {name: len(data.get(name, [])) for name in store.PLATFORMS},
    }


ROUTES = {
    ("GET", "/api/status"): api_status,
    ("GET", "/api/stats/workbuddy/credits"): api_stats_wb_credits,
    ("GET", "/api/stats/workbuddy/usage"): api_stats_wb_usage,
    ("GET", "/api/stats/trae/usage"): api_stats_trae_usage,
    ("GET", "/api/stats/trae/credits"): api_stats_trae_credits,
    ("GET", "/api/accounts"): api_accounts,
    ("POST", "/api/accounts"): api_account_add,
    ("POST", "/api/accounts/import-storage"): api_account_import_storage,
    ("POST", "/api/accounts/delete"): api_account_delete,
    ("POST", "/api/accounts/refresh-token"): api_account_refresh,
    ("POST", "/api/accounts/refresh-jwt"): api_account_refresh_jwt,
    ("POST", "/api/oauth/workbuddy/start"): api_oauth_wb_start,
    ("POST", "/api/oauth/workbuddy/poll"): api_oauth_wb_poll,
    ("POST", "/api/oauth/trae/start"): api_oauth_trae_start,
    ("POST", "/api/oauth/trae/callback"): api_oauth_trae_callback,
    ("POST", "/api/oauth/trae/callback/check"): api_oauth_trae_callback_check,
    ("POST", "/api/checkin"): api_checkin_start,
    ("GET", "/api/checkin/progress"): api_checkin_progress,
    ("GET", "/api/checkin/status"): api_checkin_status,
    ("GET", "/api/checkin/logs"): api_checkin_logs,
    ("GET", "/api/config"): api_config_get,
    ("POST", "/api/config"): api_config_save,
    ("GET", "/api/export"): api_export_config,
    ("POST", "/api/import"): api_import_config,
    ("POST", "/api/login"): api_login,
    ("POST", "/api/logout"): api_logout,
    ("GET", "/api/logs"): api_app_logs,
}

# 需要登录态的接口（口令未设置时全部放行）
PROTECTED = {route for route in ROUTES}

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def _strip_gateway_prefix(path: str) -> str:
    """剥掉统一网关可能保留的 /app/<appname> 前缀。

    飞牛桌面图标经网关转发时，部分版本会把 /app/qdassistant/... 原样透传给后端，
    部分版本会剥掉前缀只留 /...。这里统一剥掉，使两种形态都能正确路由。
    """
    prefix = f"/app/{APP_NAME.lower()}"
    if path == prefix:
        return "/"
    if path.startswith(prefix + "/"):
        return path[len(prefix):] or "/"
    # 浏览器的 HTML 预扫描会在 <base> 注入生效前，按 /app/ 基准抢先发出资源请求
    # （如 /app/style.css）。这些请求本质是本应用的资源，剥掉 /app/ 后一并兜底，
    # 避免在控制台留下一片误导性的 404 —— 页面本身其实已经正常加载。
    if path.startswith("/app/"):
        stripped = path[len("/app/"):]
        seg = stripped.split("/", 1)[0]
        if seg == "images" or seg.endswith((".css", ".js", ".png", ".ico", ".svg", ".json")):
            return "/" + stripped
    return path


class Handler(BaseHTTPRequestHandler):
    """HTTP 请求处理：API 路由 + 静态资源。"""

    server_version = f"{APP_NAME}/{APP_VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # 静音默认访问日志
        pass

    # -- 基础工具 ----------------------------------------------------------

    def _send(self, status: int, payload: bytes, content_type: str, extra_headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}):
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_json(self, status: int, data: dict, extra_headers=None) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, payload, "application/json; charset=utf-8", extra_headers)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    # -- 路由 --------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        self._dispatch({})

    def do_POST(self):  # noqa: N802
        self._dispatch(self._read_body())

    def _dispatch(self, body) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        # 统一网关转发时可能保留 /app/<appname> 前缀（各版本行为不一致），
        # 剥掉后再路由，保证剥前缀与不剥前缀两种网关都能正常工作。
        path = _strip_gateway_prefix(path)
        query = parse_qs(parsed.query)

        handler_fn = ROUTES.get((self.command, path))
        if handler_fn is None:
            if path.startswith("/api/"):
                self._send_json(404, {"error": "接口不存在"})
                return
            self._serve_static(path)
            return

        if not _authorized(self):
            self._send_json(401, {"error": "需要登录", "authRequired": True})
            return
        if self.command == "POST" and not _csrf_ok(self):
            self._send_json(403, {"error": "缺少 X-QD-Request 头（跨站请求已被拒绝）"})
            return

        try:
            result = handler_fn(query, body)
            self._send_json(200, result)
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except Exception as exc:  # 单接口异常不影响服务继续运行
            store.app_log(f"[API] {self.command} {path} 异常: {exc}")
            self._send_json(500, {"error": str(exc)})

    def _serve_static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        # 防目录穿越：规范化后必须仍在 www 目录内
        target = os.path.normpath(os.path.join(WWW_DIR, path.lstrip("/")))
        if not target.startswith(os.path.normpath(WWW_DIR)) or not os.path.isfile(target):
            self._send(404, b"Not Found", "text/plain; charset=utf-8")
            return
        content_type = STATIC_TYPES.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        try:
            with open(target, "rb") as handle:
                payload = handle.read()
        except OSError:
            self._send(404, b"Not Found", "text/plain; charset=utf-8")
            return
        # 入口页注入版本号：升级后浏览器若持有旧副本，会按旧目录结构请求资源而全 404
        if os.path.basename(target) == "index.html":
            payload = payload.replace(b"__V__", APP_VERSION.encode("utf-8"))
        self._send(200, payload, content_type)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=f"{APP_NAME} 签到助手")
    parser.add_argument("--host", default=None, help="监听地址（默认读配置，缺省 0.0.0.0）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认读配置，缺省 8790）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 ~/.qdassistant）")
    parser.add_argument("--no-open", action="store_true", help="启动后不打开浏览器")
    parser.add_argument("--no-scheduler", action="store_true", help="不启动后台签到调度")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.data_dir:
        store.set_data_dir(args.data_dir)

    cfg = store.load_config()
    host = args.host or cfg.get("host") or "0.0.0.0"
    port = args.port or int(cfg.get("port") or DEFAULT_PORT)

    if not args.no_scheduler:
        scheduler.start()

    server = ThreadingHTTPServer((host, port), Handler)
    display_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{display_host}:{port}"
    print(f"{APP_NAME} v{APP_VERSION}（签到助手）")
    print(f"Web 界面: {url}")
    print(f"数据目录: {store.data_dir()}")
    print("按 Ctrl+C 停止服务。")

    if not args.no_open:
        try:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
