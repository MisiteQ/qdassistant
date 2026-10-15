#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 账号：鉴权头构造 / token 刷新 / 惰性刷新。

红线：`X-Auth-Refresh-Source: plugin` 必须保留——两个档位的官方客户端刷新时都
发送该头，缺失时网关会把刷新判定成另一个 client 来源，国际版实测返回
`invalid_grant: Invalid refresh token`（code 12153）。
"""

from __future__ import annotations

from . import store, util, variant as wb_variant


def get_str(account: dict, key: str) -> str | None:
    value = account.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


def enterprise_id_of(account: dict) -> str | None:
    """企业版标识（两处字段都认，取其一）。"""
    for key in ("enterpriseId", "enterprise_id"):
        value = account.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def display_name(account: dict) -> str:
    """账号展示名：自定义名 > 昵称 > 邮箱 > uid > id。"""
    for key in ("name", "nickname", "email", "uid", "id"):
        value = account.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return "未命名账号"


def build_auth_headers(account: dict) -> dict:
    """构造 WorkBuddy 接口鉴权头。"""
    headers = {
        "Authorization": f"Bearer {account.get('access_token') or ''}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    uid = get_str(account, "uid")
    if uid:
        headers["X-User-Id"] = uid
    eid = enterprise_id_of(account)
    if eid:
        headers["X-Enterprise-Id"] = eid
        headers["X-Tenant-Id"] = eid
    domain = get_str(account, "domain")
    if domain:
        headers["X-Domain"] = domain
    return headers


def envelope_token_error(account: dict) -> str | None:
    """加密信封凭据短路（issue #94）。

    桌面端导出的凭据可能是 `{"$wbEncrypted": true, "envelope": "…"}` 形态，
    本服务无法解密。这里给出可读错误并引导用 OAuth/手动 token 重新添加，
    而不是发一个空 Bearer 出去被打回 401。
    """
    token = account.get("access_token")
    if isinstance(token, dict) and token.get("$wbEncrypted") is True:
        return (
            "该账号的 access_token 是加密信封格式，无法在本服务解密。"
            "请在 WorkBuddy 客户端重新登录并复制明文 token，或改用 OAuth 重新添加账号。"
        )
    return None


def refresh_url(account: dict) -> str:
    """刷新地址：必须走账号自身档位的域名。

    把国际版 token 打到国内网关等于登录失效，因此这里严格按档位取基址。
    """
    which = wb_variant.from_account(account)
    return f"{wb_variant.api_endpoint(which)}{wb_variant.api_prefix(which)}/auth/token/refresh"


def refresh_headers(account: dict, refresh_token: str) -> dict:
    """刷新请求头：鉴权头 + 刷新会话头 + 来源声明。"""
    headers = build_auth_headers(account)
    headers["X-Refresh-Token"] = refresh_token
    headers["X-Auth-Refresh-Source"] = "plugin"
    return headers


def refresh_account_token(account: dict) -> dict:
    """刷新单账号 token；成功则落盘并返回新账号。

    刷新失败（refresh token 失效等）时给账号标记 `needs_relogin`，避免无限重试。
    """
    account = dict(account)
    refresh_token = account.get("refresh_token") or ""
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        account["needs_relogin"] = True
        account["needs_relogin_reason"] = "缺少 refresh token，无法刷新，需重新登录"
        store.upsert_account(store.PLATFORM_WORKBUDDY, account)
        return account

    resp = util.http_json(
        refresh_url(account),
        "POST",
        body={},
        headers=refresh_headers(account, refresh_token),
    )
    code = _as_int(resp.get("code"), -1)
    if code not in (0, 200):
        message = resp.get("message") or resp.get("msg") or "未知错误"
        account["needs_relogin"] = True
        account["needs_relogin_reason"] = f"刷新失败(code={code}): {message}"
        store.upsert_account(store.PLATFORM_WORKBUDDY, account)
        return account

    data = resp.get("data")
    data = data if isinstance(data, dict) else {}
    new_at = data.get("accessToken") or data.get("access_token")
    if not isinstance(new_at, str) or not new_at:
        account["needs_relogin"] = True
        account["needs_relogin_reason"] = "刷新响应缺少 accessToken"
        store.upsert_account(store.PLATFORM_WORKBUDDY, account)
        return account

    account["access_token"] = new_at
    for src, dst in (
        ("refreshToken", "refresh_token"),
        ("refresh_token", "refresh_token"),
        ("expiresAt", "expiresAt"),
        ("expires_at", "expiresAt"),
    ):
        value = data.get(src)
        if value is not None:
            account[dst] = value
    account.pop("needs_relogin", None)
    account.pop("needs_relogin_reason", None)
    store.upsert_account(store.PLATFORM_WORKBUDDY, account)
    return account


def ensure_fresh_token(account: dict, cfg: dict | None = None) -> dict:
    """惰性刷新：token 已过期或剩余不足阈值时刷新一次。

    `expiresAt` 缺失视为过期（stale=True），此时只要带 refresh_token 就刷新。
    """
    section = store.platform_config(store.PLATFORM_WORKBUDDY, cfg)
    lazy_hours = _as_int(section.get("lazy_refresh_hours"), 24)
    expires_at = util.norm_ts(account.get("expiresAt"))
    if expires_at is None:
        stale = True
    else:
        stale = util.now_ms() >= expires_at or (expires_at - util.now_ms()) < lazy_hours * 3600 * 1000
    has_rt = isinstance(account.get("refresh_token"), str) and bool(account["refresh_token"].strip())
    if stale and has_rt:
        return refresh_account_token(account)
    return account


def _as_int(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default
