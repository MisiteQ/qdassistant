#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OAuth 登录采集：两个平台各一条链路。

**WorkBuddy**：服务端纯轮询的「扫码 / 浏览器授权」流程，无需任何本地客户端——天然适配 NAS：

    POST {endpoint}/v2/plugin/auth/state?platform=<platform>   → state + authUrl
    用户在浏览器完成授权
    GET  {endpoint}/v2/plugin/auth/token?state=<state>         → accessToken
    GET  {endpoint}/v2/plugin/login/account?state=<state>      → uid/nickname/email

**Trae**：授权页 302 回调指向 `http://127.0.0.1:17388/authorize`——回调发生在**用户浏览器
所在机器**，NAS 上没有 Trae 客户端可接。因此采用「复制回调 URL」闭环：

    1. 服务端生成登录 URL（PKCE + login_trace_id 绑定），用户在浏览器打开并授权；
    2. 授权完成后浏览器跳到 127.0.0.1 的打不开页面，用户复制地址栏完整 URL；
    3. 粘贴回本服务 → 解析回调参数 → 换取 JWT。

Token 交换只实现**不需要设备签名**的协议形态（ClientID + RefreshToken +
ClientSecret 旧协议）。带 DeviceProof / 设备公钥的变体需要从本机 Trae 客户端
storage.json 提取 ECDSA 私钥，NAS 上不存在该客户端，故不实现；换取失败时
回落到「手动粘贴 JWT」路径（账号页始终保留）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from urllib.parse import urlencode

from . import store, util, variant as wb_variant

# WorkBuddy OAuth 轮询有效期（与 OAUTH_TIMEOUT_SECONDS 保持一致）
OAUTH_TIMEOUT_SECONDS = 600

# Trae AuthCode 有效期：授权服务返回 ExpireDuration=600000（毫秒），即 10 分钟。
# 超时后交换必然失败——这里用于「提交前预判 + 失败时给出可读提示」，
# 避免用户看到一串协议错误堆栈却不知道该重新授权。
TRAE_AUTHCODE_TTL_SECONDS = 600

# 过期判定宽容量：服务端时钟与本地可能有偏差，差几秒就报过期反而误伤
TRAE_EXPIRY_GRACE_SECONDS = 15


class OAuthExpiredError(ValueError):
    """AuthCode 已过期（或无从判断时效时会话已超时）——提示重新发起登录。"""

# ---------------------------------------------------------------------------
# Trae OAuth 常量（2026-09-16 抓包固化）
# ---------------------------------------------------------------------------

TRAE_CLIENT_ID = "ono9krqynydwx5"
TRAE_CLIENT_SECRET = "-"
TRAE_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
TRAE_PAGE_PLUGIN_VERSION = "2.3.83560"
TRAE_PAGE_APP_VERSION = "3.3.100"
TRAE_PLATFORM_CODE = "IDE_PC"
TRAE_REDIRECT_URI = "http://127.0.0.1:17388/authorize"
TRAE_EXCHANGE_URL = "https://api.trae.com.cn/cloudide/api/v3/trae/oauth/ExchangeToken"
TRAE_AUTH_PAGE = "https://www.trae.cn/authorization"

# ExchangeToken 端点 host 白名单：host 来自用户粘贴的回调 URL 参数，直接拼接
# 成交换端点——AuthCode / PKCE 均属安全令牌，必须限定官方 API 域，防伪造回调
# 把令牌发往任意第三方主机。
TRAE_EXCHANGE_HOST_ALLOWLIST = ("api.trae.com.cn", "api.trae.cn", "api.trae.com")

# ---------------------------------------------------------------------------
# 会话状态（内存，进程重启即失效——重新发起登录即可）
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_WB_STATES: dict[str, dict] = {}
_TRAE_STATES: dict[str, dict] = {}


# ===========================================================================
# WorkBuddy OAuth
# ===========================================================================


def _wb_state_url(which: str) -> str:
    platform = "workbuddy-ai" if which == wb_variant.AI else "workbuddy"
    return f"{wb_variant.api_endpoint(which)}{wb_variant.api_prefix(which)}/auth/state?platform={platform}"


def _wb_token_url(which: str, state: str) -> str:
    return f"{wb_variant.api_endpoint(which)}{wb_variant.api_prefix(which)}/auth/token?state={state}"


def _wb_account_url(which: str, state: str) -> str:
    return f"{wb_variant.api_endpoint(which)}{wb_variant.api_prefix(which)}/login/account?state={state}"


def wb_oauth_start(which: str = "cn") -> dict:
    """发起 WorkBuddy OAuth 登录：向官方申请 state，返回 loginId / authUrl。

    用户在任意设备浏览器打开 authUrl 授权；本服务随后轮询 token 接口直到完成。
    """
    which = wb_variant.parse(which)
    login_id = f"wb_{secrets.token_hex(16)}"
    resp = util.http_json(_wb_state_url(which), "POST", body={})
    data = resp.get("data")
    data = data if isinstance(data, dict) else {}
    state = data.get("state")
    if not isinstance(state, str) or not state:
        snippet = json.dumps(resp, ensure_ascii=False)[:300]
        raise ValueError(f"auth/state 响应缺少 state: {snippet}")

    auth_url = data.get("authUrl") or data.get("auth_url") or data.get("url")
    if not isinstance(auth_url, str) or not auth_url:
        auth_url = f"{wb_variant.api_endpoint(which)}/login?state={state}"

    with _LOCK:
        _WB_STATES[login_id] = {
            "variant": which,
            "state": state,
            "expires_at": int(time.time()) + OAUTH_TIMEOUT_SECONDS,
            "done": False,
            "result": None,
            "error": None,
        }
    return {
        "loginId": login_id,
        "verificationUri": auth_url,
        "expiresIn": OAUTH_TIMEOUT_SECONDS,
    }


def _wb_domain_mismatch_error(which: str, domain: str) -> str | None:
    """档位一致性校验：响应 `domain` 与发起档位不符时拒绝入库。

    国际版 state 换回国内版 token（或反之）时，`domain` 会与后续请求的
    Origin/X-Domain 不一致，网关按一致性直接拒绝。
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return None
    if which == wb_variant.AI:
        ok = domain.endswith(wb_variant.AI_DOMAIN_SUFFIX)
    else:
        ok = not domain.endswith(wb_variant.AI_DOMAIN_SUFFIX)
    if ok:
        return None
    label = "国际版" if which == wb_variant.AI else "国内版"
    return f"登录响应的 domain（{domain}）与{label}档位不符，已拒绝入库"


def wb_oauth_poll(login_id: str) -> dict:
    """轮询一次官方 token 接口。成功则拉取账号信息并入库。"""
    with _LOCK:
        info = _WB_STATES.get(login_id)
        if not info:
            return {"done": True, "error": "登录请求不存在"}
        if info["done"]:
            return {"done": True, "result": info["result"], "error": info["error"]}
        if time.time() > info["expires_at"]:
            info["done"] = True
            info["error"] = "登录超时"
            return {"done": True, "error": "登录超时"}
        which = info["variant"]
        state = info["state"]

    resp = util.http_json(_wb_token_url(which, state), "GET")
    code = resp.get("code")
    code = code if isinstance(code, int) else -1
    if code not in (0, 200):
        return {"done": False}
    data = resp.get("data")
    data = data if isinstance(data, dict) else {}
    access_token = data.get("accessToken") or data.get("access_token") or ""
    if not isinstance(access_token, str) or not access_token:
        return {"done": False}

    domain = data.get("domain") or ""
    error = _wb_domain_mismatch_error(which, domain)
    if error:
        return _wb_fail(login_id, error)

    # 拉取账号信息
    headers = {"Authorization": f"Bearer {access_token}"}
    if domain:
        headers["X-Domain"] = domain
    acc_resp = util.http_json(_wb_account_url(which, state), "GET", headers=headers)
    acc_data = acc_resp.get("data")
    acc_data = acc_data if isinstance(acc_data, dict) else {}

    profile_domain = acc_data.get("domain") or ""
    error = _wb_domain_mismatch_error(which, profile_domain)
    if error:
        return _wb_fail(login_id, error)

    expires_at = util.norm_ts(data.get("expiresAt") or data.get("expires_at"))
    if expires_at is None and isinstance(data.get("expiresIn"), int):
        expires_at = util.now_ms() + data["expiresIn"] * 1000
    refresh_expires_at = util.norm_ts(
        data.get("refreshExpiresAt") or data.get("refresh_expires_at")
    )
    if refresh_expires_at is None and isinstance(data.get("refreshExpiresIn"), int):
        refresh_expires_at = util.now_ms() + data["refreshExpiresIn"] * 1000

    email = acc_data.get("email")
    email = email if isinstance(email, str) and email.strip() else None

    account = {
        "uid": acc_data.get("uid"),
        "nickname": acc_data.get("nickname"),
        "email": email,
        "enterpriseName": acc_data.get("enterpriseName"),
        "enterpriseId": acc_data.get("enterpriseId"),
        "access_token": access_token,
        "refresh_token": data.get("refreshToken") or data.get("refresh_token") or "",
        "token_type": data.get("tokenType") or data.get("token_type") or "Bearer",
        "domain": domain,
        # 档位随登录来源写入：后续刷新/签到都以它为准
        "variant": which,
        "expiresAt": expires_at,
        "refreshExpiresAt": refresh_expires_at,
        "auth_raw": data,
        "profile_raw": acc_data,
        "createdAt": util.now_ms(),
    }
    saved = store.upsert_account(store.PLATFORM_WORKBUDDY, account)
    result = {"id": saved.get("id"), "name": saved.get("nickname") or saved.get("email") or saved.get("uid")}
    with _LOCK:
        info = _WB_STATES.get(login_id)
        if info:
            info["done"] = True
            info["result"] = result
    return {"done": True, "result": result}


def _wb_fail(login_id: str, error: str) -> dict:
    with _LOCK:
        info = _WB_STATES.get(login_id)
        if info:
            info["done"] = True
            info["error"] = error
    return {"done": True, "error": error}


# ===========================================================================
# Trae OAuth
# ===========================================================================


def _pkce_pair() -> tuple[str, str]:
    """PKCE：verifier（64 位 hex）+ S256 challenge（base64url 无填充）。"""
    verifier = secrets.token_hex(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _trae_device() -> dict:
    """登录用设备指纹：从既有账号库选一台已派生的，没有则新派生。

    遵循「登录设备与签到设备同源」原则——device_map 里已有条目与新 JWT 的
    设备指纹校验匹配，优先沿用。
    """
    from . import trae_identity

    mapping = store.load_device_map()
    for uid, entry in mapping.items():
        if isinstance(entry, dict) and entry.get("device_id"):
            return entry
    device = trae_identity.derive_device(f"qda-{util.now_ms()}-{secrets.token_hex(4)}")
    return device


def trae_oauth_start() -> dict:
    """生成 Trae OAuth 登录 URL（PKCE + login_trace_id 绑定）。

    返回 `{"url", "state", "redirectUri", "authCodeTtl"}`；`redirectUri` 告知前端
    授权后浏览器会跳到哪里（127.0.0.1 打不开是预期行为，复制地址栏 URL 粘贴
    回来即可），`authCodeTtl` 用于前端做回填倒计时（AuthCode 只有 10 分钟）。
    """
    import platform as _platform
    import socket

    device = _trae_device()
    trace_id = util.random_hex(32)
    verifier, challenge = _pkce_pair()

    try:
        hostname = socket.gethostname() or "QDA"
    except OSError:
        hostname = "QDA"
    if _platform.system() == "Windows":
        device_type, os_version = "windows", "Windows"
    elif _platform.system() == "Darwin":
        device_type, os_version = "mac", "macOS"
    else:
        device_type, os_version = "linux", "Linux"

    params = {
        "login_version": "1",
        "auth_from": "trae",
        "login_channel": "native_ide",
        "plugin_version": TRAE_PAGE_PLUGIN_VERSION,
        "auth_type": "local",
        "client_id": TRAE_CLIENT_ID,
        "redirect": "0",
        "login_trace_id": trace_id,
        "auth_callback_url": TRAE_REDIRECT_URI,
        "machine_id": device.get("session_id") or "",
        "device_id": device.get("device_id") or "",
        "x_device_id": device.get("device_id") or "",
        "x_machine_id": device.get("session_id") or "",
        "x_device_brand": hostname,
        "x_device_type": device_type,
        "x_os_version": os_version,
        "x_env": "",
        "x_app_version": TRAE_PAGE_APP_VERSION,
        "x_app_type": "stable",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "channel_name": "common",
    }
    url = f"{TRAE_AUTH_PAGE}?{urlencode(params)}"

    with _LOCK:
        _TRAE_STATES[trace_id] = {
            "state": trace_id,
            "pkce_verifier": verifier,
            "device": device,
            "created_at": int(time.time()),
            "expires_at": int(time.time()) + OAUTH_TIMEOUT_SECONDS,
        }
    return {
        "url": url,
        "state": trace_id,
        "redirectUri": TRAE_REDIRECT_URI,
        "authCodeTtl": TRAE_AUTHCODE_TTL_SECONDS,
    }


def _authcode_age(auth_json: dict, session: dict | None) -> tuple[int | None, int]:
    """估算 AuthCode 已存活秒数与有效期；无法判断时 age 返回 None。

    时效优先取回调自带的字段（`ExpireDuration` 毫秒 / `CreateAt` 时间戳），
    缺失时回落到本进程记录的登录发起时间——异地授权场景里两者通常一致。
    """
    ttl = TRAE_AUTHCODE_TTL_SECONDS
    raw_ttl = auth_json.get("ExpireDuration") or auth_json.get("expireDuration")
    if isinstance(raw_ttl, (int, float)) and raw_ttl > 0:
        # 毫秒值（600000）与秒值（600）都要能识别
        ttl = int(raw_ttl // 1000) if raw_ttl > 10000 else int(raw_ttl)

    now = int(time.time())
    started = None
    for key in ("CreateAt", "CreatedAt", "createAt", "Timestamp", "timestamp"):
        raw = auth_json.get(key)
        if isinstance(raw, (int, float)) and raw > 0:
            started = int(raw // 1000) if raw > 1e11 else int(raw)
            break
    if started is None and session:
        started = int(session.get("created_at") or 0) or None
    if started is None:
        return None, ttl
    return max(0, now - started), ttl


def trae_callback_precheck(callback_url: str) -> dict:
    """粘贴内容即时校验（纯本地，不发任何网络请求）。

    前端每次输入都调一次，让「粘错了 / 粘漏了 / 已过期」在提交前就暴露，
    而不是等一次必然失败的交换。返回：

        {"ok": bool, "kind": "jwt|refresh|authcode|unknown",
         "reason": str, "expired": bool, "remainingSeconds": int|None}
    """
    empty = {"ok": False, "kind": "unknown", "reason": "", "expired": False, "remainingSeconds": None}
    url = (callback_url or "").strip()
    if not url:
        return {**empty, "reason": "还没有粘贴内容"}
    if "://" not in url and "?" not in url:
        return {**empty, "reason": "看起来不是一条完整 URL（应以 http://127.0.0.1:17388 开头）"}

    try:
        params = _parse_callback_params(url)
    except ValueError as exc:
        return {**empty, "reason": str(exc)}

    kind = "unknown"
    if params.get("accessToken") or params.get("access_token"):
        kind = "jwt"
    elif params.get("refreshToken") or params.get("refresh_token"):
        kind = "refresh"
    elif params.get("authCodeInfo") or params.get("code"):
        kind = "authcode"
    else:
        return {
            **empty,
            "reason": "URL 里没有可用的凭证（应含 authCodeInfo / accessToken / refreshToken 之一）——"
            "请从浏览器地址栏完整复制，注意别丢掉 %7B 开头的编码段",
        }

    # AuthCode 走的是交换路径，有时效；直接带 JWT/refresh 的不受 10 分钟限制
    if kind != "authcode":
        return {**empty, "ok": True, "kind": kind, "reason": "凭证已识别，可提交"}

    raw_auth = params.get("authCodeInfo") or ""
    auth_json: dict = {}
    if raw_auth:
        try:
            parsed = json.loads(raw_auth)
            auth_json = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {**empty, "kind": kind, "reason": "authCodeInfo 不是合法 JSON——地址栏复制时可能被截断了"}
    if params.get("code"):
        return {**empty, "ok": True, "kind": kind, "reason": "凭证已识别，可提交"}

    code = auth_json.get("AuthCode")
    if not isinstance(code, str) or not code:
        return {**empty, "kind": kind, "reason": "authCodeInfo 里没有 AuthCode 字段——请重新复制地址栏完整 URL"}

    session = _latest_trae_session()
    age, ttl = _authcode_age(auth_json, session)
    if age is None:
        return {**empty, "ok": True, "kind": kind, "reason": "凭证已识别，可提交"}
    remaining = ttl - age
    if remaining <= 0:
        return {
            **empty,
            "kind": kind,
            "expired": True,
            "remainingSeconds": 0,
            "reason": f"AuthCode 已过期（有效期 {ttl // 60} 分钟）——请重新点击「OAuth 登录」发起授权",
        }
    return {
        **empty,
        "ok": True,
        "kind": kind,
        "remainingSeconds": remaining,
        "reason": f"AuthCode 有效，剩余约 {remaining // 60} 分 {remaining % 60} 秒",
    }


def _assert_authcode_usable(auth_json: dict, session: dict | None) -> None:
    """AuthCode 交换前的时效闸门：过期抛 OAuthExpiredError（可读提示）。"""
    age, ttl = _authcode_age(auth_json or {}, session)
    if age is None:
        return
    if age > ttl + TRAE_EXPIRY_GRACE_SECONDS:
        raise OAuthExpiredError(
            f"AuthCode 已过期（有效期约 {ttl // 60} 分钟，距今已 {age // 60} 分钟）——"
            "请重新点击「OAuth 登录」发起授权，并在 10 分钟内回填回调 URL"
        )


def _latest_trae_session() -> dict | None:
    """取最近一个仍在有效期内的 Trae 登录会话（用于时效判断 / PKCE 取值）。"""
    now = time.time()
    with _LOCK:
        for value in reversed(list(_TRAE_STATES.values())):
            if value.get("expires_at", 0) > now:
                return value
    return None


def _parse_callback_params(callback_url: str) -> dict:
    """解析回调 URL 的查询参数（URL 解码）。"""
    query = callback_url.split("?", 1)
    if len(query) < 2:
        raise ValueError("回调 URL 中缺少查询参数")
    params: dict[str, str] = {}
    for pair in query[1].split("#")[0].split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        value = value.replace("+", " ")
        # 手写 percent 解码（避免引入 urllib.parse.unquote_plus 之外的差异）
        from urllib.parse import unquote

        params[key] = unquote(value)
    return params


def trae_oauth_callback(callback_url: str) -> dict:
    """解析用户粘贴的回调 URL，完成 token 交换并入库。

    支持三种回调形态（按优先级）：
    1. 直接携带 `accessToken`（JWT）——最简形态，无需交换；
    2. 携带 `refreshToken` ——旧协议交换（ClientID + ClientSecret，无需设备签名）；
    3. 携带 `authCodeInfo.AuthCode` ——PKCE 交换（尽力而为，NAS 无设备私钥时
       可能被服务端拒绝，报错引导改用 JWT 粘贴）。

    返回入库后的账号信息。以 **JWT 解析出的 user_id 为权威**（回调 URL 参数可被
    篡改；JWT 由授权服务签发不可篡改），两者不一致时拒绝。
    """
    params = _parse_callback_params(callback_url)

    # 身份信息提取（authCodeInfo / userInfo JSON 补全）
    auth_code = None
    auth_json: dict = {}
    raw_auth = params.get("authCodeInfo")
    if raw_auth:
        try:
            auth_json = json.loads(raw_auth)
        except ValueError as exc:
            raise ValueError(f"authCodeInfo 解析失败（{exc}）：授权页回调格式异常")
        code = auth_json.get("AuthCode")
        if not isinstance(code, str) or not code:
            raise ValueError("authCodeInfo 中缺少 AuthCode 字段")
        auth_code = code
    raw_user = params.get("userInfo")
    if raw_user:
        try:
            user_json = json.loads(raw_user)
            if isinstance(user_json, dict):
                params.setdefault("UserID", user_json.get("UserID") or "")
                params.setdefault("userName", user_json.get("ScreenName") or user_json.get("NickName") or "")
                params.setdefault("avatar", user_json.get("AvatarUrl") or "")
        except ValueError:
            pass

    callback_uid = (
        params.get("userId")
        or params.get("user_id")
        or params.get("UserID")
        or None
    )
    callback_name = params.get("userName") or params.get("user_name") or params.get("nickname") or None

    # CSRF 绑定：回调携带 loginTraceID 且本进程有在途会话时必须一致
    cb_trace = params.get("loginTraceID") or params.get("login_trace_id")
    session = None
    with _LOCK:
        if cb_trace:
            session = _TRAE_STATES.get(cb_trace)
        else:
            # 无 trace 时取最近一个未过期的在途会话
            for value in _TRAE_STATES.values():
                if value["expires_at"] > time.time():
                    session = value
    if cb_trace and session and session["state"] != cb_trace:
        raise ValueError(
            "OAuth loginTraceID 校验失败：回调 URL 与本服务发起的登录请求不匹配"
            "（可能为伪造或重放），已拒绝"
        )

    # 1) 回调直接带 accessToken
    access_token = params.get("accessToken") or params.get("access_token") or ""
    refresh_token = params.get("refreshToken") or params.get("refresh_token") or ""

    # 2) 无 accessToken → 用 refreshToken / AuthCode 交换
    if not access_token:
        if refresh_token:
            access_token, refresh_token = trae_exchange_refresh(refresh_token)
        elif auth_code or params.get("code"):
            # 过期就别发出这次必然失败的交换了，直接给用户可读的重来指引
            _assert_authcode_usable(auth_json, session)
            code = auth_code or params.get("code")
            verifier = (session or {}).get("pkce_verifier") if session else None
            host = params.get("host") or "https://api.trae.com.cn"
            access_token, refresh_token = trae_exchange_authcode(code, verifier, host)
        else:
            raise ValueError("回调 URL 中缺少 accessToken/refreshToken/authCodeInfo 参数")

    if not access_token:
        raise ValueError("token 交换失败：未取得 accessToken")

    jwt = access_token if access_token.startswith("Cloud-IDE-JWT ") else f"Cloud-IDE-JWT {access_token}"

    # 权威 uid：以 JWT 解析结果为准
    from . import trae_identity

    info = trae_identity.jwt_parse(jwt)
    jwt_uid = info["user_id"]
    if not jwt_uid:
        raise ValueError("无法从交换到的 JWT 中解析 user_id")
    if callback_uid and callback_uid.strip() and callback_uid.strip() != jwt_uid:
        raise ValueError(
            "回调 URL 中的账号 uid 与凭证 JWT 解析结果不一致，已拒绝——"
            "请确认回调 URL 完整未被修改"
        )

    # 同 uid 更新既有账号，保留用户起的名字
    name = callback_name or jwt_uid
    existing = None
    for acc in store.load_accounts(store.PLATFORM_TRAE):
        if acc.get("user_id") == jwt_uid:
            existing = acc
            name = acc.get("name") or name
            break

    account = {
        "jwt": jwt,
        "user_id": jwt_uid,
        "name": name,
    }
    if refresh_token:
        account["refresh_token"] = refresh_token
    if existing:
        account["id"] = existing.get("id")
    if params.get("avatar"):
        account["avatar"] = params["avatar"]
    saved = store.upsert_account(store.PLATFORM_TRAE, account)
    return {
        "id": saved.get("id"),
        "userId": jwt_uid,
        "name": name,
        "merged": existing is not None,
        "expHours": round(info["exp_hours"], 1) if info["exp_hours"] is not None else None,
    }


def _find_token_value(body, keys: tuple) -> str | None:
    """全树深挖第一个命中的 token 字段（Result/data/Data 容器均可）。"""
    if isinstance(body, dict):
        for key in keys:
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in body.values():
            found = _find_token_value(value, keys)
            if found:
                return found
    elif isinstance(body, list):
        for value in body:
            found = _find_token_value(value, keys)
            if found:
                return found
    return None


def _volcano_error(body: dict):
    """火山信封 / 类火山错误分流：返回 (code, message, std_code) 或 None。

    支持形态：
    - { "Error": { "Code": "20403", "Message": "..." } }
    - { "error": { "code": "...", "message": "..." } }
    - { "code": "20403", "message": "..." } 或 { "Code": "20403", "Message": "..." }
    - { "err_code": "20403", "err_msg": "..." }
    """
    if not isinstance(body, dict):
        return None

    # 嵌套 error / Error
    err = body.get("Error") or body.get("error")
    if isinstance(err, dict):
        code = str(err.get("Code") or err.get("code") or "")
        message = str(err.get("Message") or err.get("message") or "")
        std_code = err.get("CodeNo") or err.get("std_code") or ""
        if code and code != "0":
            return (code, message, std_code)

    # 平铺字段
    for code_key, msg_key in (
        ("Code", "Message"),
        ("code", "message"),
        ("err_code", "err_msg"),
        ("error_code", "error_msg"),
        ("errno", "errmsg"),
    ):
        code = body.get(code_key)
        message = body.get(msg_key)
        if code is not None:
            code = str(code)
            if code and code != "0":
                return (code, str(message or ""), "")
    return None


def _exchange_post(url: str, payload: dict, device_id: str, empty_token_header: bool = True) -> dict:
    """ExchangeToken 统一请求：设备头 + 空鉴权头，返回解析后的 JSON。"""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("content-type", "application/json")
    request.add_header("accept", "*/*")
    request.add_header("x-device-id", device_id or "")
    request.add_header("x-app-id", TRAE_APP_ID)
    request.add_header("x-platform-code", TRAE_PLATFORM_CODE)
    if empty_token_header:
        # F-70 实测：x-cloudide-token 必须为空字符串
        request.add_header("x-cloudide-token", "")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=120) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
    except Exception as exc:
        raise ValueError(f"请求失败: {exc}") from exc
    try:
        return json.loads(raw)
    except ValueError:
        raise ValueError(f"响应非 JSON: {raw[:200]}")


def _validate_exchange_host(host: str) -> str:
    """host 白名单校验（防伪造回调把令牌发往第三方）；返回规范化 host。"""
    bare = (host or "").strip()
    if "://" in bare:
        bare = bare.split("://", 1)[1]
    bare = bare.strip("/").split("/")[0].lower()
    if bare not in TRAE_EXCHANGE_HOST_ALLOWLIST:
        raise ValueError(f"交换端点 host 不在白名单内: {bare}")
    return f"https://{bare}"


def trae_exchange_refresh(refresh_token: str) -> tuple[str, str]:
    """用 refresh_token 换取 accessToken（旧协议：ClientID + ClientSecret）。

    返回 `(access_token, refresh_token)`；服务端不轮换 refresh 时沿用旧值。
    该变体不需要设备签名，是 NAS 场景下唯一可用的刷新路径。
    """
    device = _trae_device()
    payload = {
        "ClientID": TRAE_CLIENT_ID,
        "RefreshToken": refresh_token,
        "ClientSecret": TRAE_CLIENT_SECRET,
        "UserID": "",
    }
    body = _exchange_post(TRAE_EXCHANGE_URL, payload, device.get("device_id", ""))
    store.app_log("[Trae OAuth] refresh 交换响应已接收")

    volcano = _volcano_error(body)
    if volcano:
        code, message, _std = volcano
        raise ValueError(f"code={code}: {message or '未知错误'}")
    if isinstance(body.get("code"), int) and body["code"] != 0:
        raise ValueError(f"code={body['code']}: {body.get('message') or '未知错误'}")

    access = _find_token_value(body, ("AccessToken", "access_token", "token", "Jwt", "JWT"))
    refresh = _find_token_value(body, ("RefreshToken", "refresh_token")) or refresh_token
    if not access:
        raise ValueError("响应中未找到 AccessToken 字段")
    return access, refresh


def trae_exchange_authcode(code: str, verifier: str | None, host: str) -> tuple[str, str]:
    """用 AuthCode 交换 token（PKCE 主路径，尽力而为）。

    真实客户端形态需要 DeviceInfo.DevicePublicKey（本机 Trae 客户端私钥派生），
    NAS 上不存在该客户端；这里发送无公钥的最小形态，服务端若要求设备凭证会
    返回设备类错误（20403/20405），此时引导用户改用「手动粘贴 JWT」。
    """
    base = _validate_exchange_host(host)
    url = f"{base}/trae/api/v3/oauth/ExchangeToken"
    device = _trae_device()
    payload = {
        "ClientID": TRAE_CLIENT_ID,
        "AuthCode": code,
        "CodeVerifier": verifier or "",
        "DeviceID": device.get("device_id", ""),
        "PlatformCode": TRAE_PLATFORM_CODE,
    }
    body = _exchange_post(url, payload, device.get("device_id", ""))
    volcano = _volcano_error(body)
    if volcano:
        code_str, message, _std = volcano
        if "20403" in code_str or "20405" in code_str or "Device" in (message or ""):
            raise ValueError(
                f"AuthCode 交换被服务端拒绝（code={code_str}: {message}）——"
                "该路径需要本机 Trae 客户端的设备凭证，NAS 上不可用；"
                "请改用「手动粘贴 JWT」添加账号"
            )
        raise ValueError(f"code={code_str}: {message or '未知错误'}")
    if isinstance(body.get("code"), int) and body["code"] != 0:
        raise ValueError(f"code={body['code']}: {body.get('message') or '未知错误'}")

    access = _find_token_value(body, ("AccessToken", "access_token", "token", "Jwt", "JWT"))
    refresh = _find_token_value(body, ("RefreshToken", "refresh_token")) or ""
    if not access:
        # 无 token 时优先把服务端拒绝理由readable化；设备签名类直接引导改用手动粘贴 JWT
        volcano = _volcano_error(body)
        if volcano:
            code_str, message, _std = volcano
            text = message or ""
            if "20403" in code_str or "20405" in code_str or "Device" in text or "device" in text.lower() or "签名" in text or "proof" in text.lower():
                raise ValueError(
                    f"AuthCode 交换被服务端拒绝（code={code_str}: {text}）——"
                    "该路径需要本机 Trae 客户端的设备凭证，NAS 上不可用；"
                    "请改用「手动粘贴 JWT」添加账号"
                )
            raise ValueError(f"code={code_str}: {text or '未知错误'}")
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))[:300]
        lower_raw = raw.lower()
        if "device" in lower_raw or "proof" in lower_raw or "20403" in raw or "20405" in raw:
            raise ValueError(
                "Trae 服务端未返回有效凭证，且响应里出现设备签名相关关键字 ——"
                "该路径需要本机 Trae 客户端的设备凭证，NAS 上不可用；"
                "请改用「手动粘贴 JWT」添加账号"
            )
        raise ValueError(f"Trae 服务端未返回有效凭证：{raw}")
    return access, refresh


def ensure_fresh_trae_jwt(account: dict) -> dict:
    """JWT 缺失 / 过期且带 refresh_token 时自动刷新一次。

    供签到调度与手动「刷新 JWT」按钮共用；刷新失败保持原状（签到会按
    SessionDead 分类进冷却，不会无限重试）。
    """
    from . import trae_identity

    account = dict(account)
    jwt = str(account.get("jwt") or "").strip()
    refresh_token = str(account.get("refresh_token") or "").strip()
    if not refresh_token:
        return account
    info = trae_identity.jwt_parse(jwt)
    if info["exp_hours"] is not None and info["exp_hours"] > 2.0:
        return account  # 还没到刷新阈值
    try:
        access, refreshed = trae_exchange_refresh(refresh_token)
    except ValueError as exc:
        store.app_log(f"[Trae OAuth] JWT 自动刷新失败: {exc}")
        account["refresh_error"] = str(exc)
        return account
    new_jwt = access if access.startswith("Cloud-IDE-JWT ") else f"Cloud-IDE-JWT {access}"
    account["jwt"] = new_jwt
    account["refresh_token"] = refreshed
    account.pop("refresh_error", None)
    store.upsert_account(store.PLATFORM_TRAE, account)
    store.app_log("[Trae OAuth] JWT 已自动刷新")
    return account
