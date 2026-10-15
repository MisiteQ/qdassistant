#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trae 多账号签到引擎。

- 直连 `api.trae.cn`（不读系统/环境代理）
- status 预检（已签跳过）→ claim（仅网络异常按 retry 次数重试，业务失败不重试）
  → 错误分类 → 冷却落盘
- 积分归属三层兜底（claim 奖励字段 → 复查余额差值 → 旧行为）
- 侧写文件：`trae_cooldowns.json` / `credits_history.json` / `checkin_summary.json`
- 红线：JWT 不进日志/事件（日志只含 user_id / name / status 等脱敏字段）
"""

from __future__ import annotations

import time

from . import store, util
from .oauth import ensure_fresh_trae_jwt
from .trae_identity import EXPIRY_WARN_HOURS, get_device_for, jwt_parse

# 接口地址
SIGNIN_URL = "https://api.trae.cn/trae/api/v2/ug/checkin_credits/claim"
STATUS_URL = "https://api.trae.cn/trae/api/v2/ug/checkin_credits/status"

# 网络异常重试间隔（与 RETRY_GAP 保持一致）
RETRY_GAP = 1

# claim 响应中疑似「本次奖励」的候选字段（按优先级排列；data 层优先于顶层）
CLAIM_REWARD_KEYS = (
    "reward",
    "reward_credits",
    "claim_credits",
    "checkin_credits",
    "delta",
    "increase",
    "obtain",
    "gained",
    "credits",
    "amount",
)

# 信封包裹键（外层 → 内层依次下钻）
PAYLOAD_WRAPPER_KEYS = ("data", "result", "resp", "response", "info")
# 下钻限深（防异常嵌套）
UNWRAP_MAX_DEPTH = 8

PLATFORM = store.PLATFORM_TRAE

# 永久冷却哨兵：一个足够远的未来时间戳，视为「永不自动解封」
FOREVER = 9_999_999_999


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------


def build_headers(jwt: str, device: dict) -> dict:
    """签到/状态接口共用请求头（按账号独立设备 id 与 session）。"""
    auth = jwt if jwt.startswith("Cloud-IDE-JWT ") else f"Cloud-IDE-JWT {jwt.strip()}"
    return {
        "accept": "*/*",
        "accept-language": "zh-CN",
        "authorization": auth,
        "content-type": "application/json",
        "user-agent": "VSCode 1.107.1 (TRAE SOLO CN)",
        "x-market-client-id": "VSCode 1.107.1",
        "x-market-user-id": device.get("market_user_id") or "",
        "x-user-region": "CN",
        "x-device-id": device.get("device_id") or "",
        "x-lgw-req-sdk-type": "3",
        "package-type": "stable_cn",
        "x-request-id": util.random_hex(32),
        "x-lscbd-aid": "787976",
        "x-lscbd-platform": "windows",
        "app-version": "0.1.45",
        "x-tt-trace-id": f"00-{util.random_hex(16)}-01",
        "vscode-sessionid": device.get("session_id") or "",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "none",
    }


def http_post(jwt: str, device: dict, url: str, timeout: int = 30) -> tuple[int, dict | None, str]:
    """统一 POST 入口 → `(http_status, parsed, raw_text)`。

    status 0 = 网络异常。复用 `util.http_json` 的代理禁用与超时策略，但这里需要
    保留原始文本用于「非 JSON 响应」排查，因此额外返回 raw。
    """
    headers = build_headers(jwt, device)
    request_headers = {key: value for key, value in headers.items()}
    request_headers["Accept"] = "*/*"

    import json as _json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url, data=b"{}", method="POST", headers=request_headers
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                return (200, _json.loads(raw), raw)
            except ValueError:
                return (200, None, raw)
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            return (int(exc.code), _json.loads(raw), raw)
        except ValueError:
            return (int(exc.code), None, raw)
    except Exception as exc:
        return (0, None, str(exc) if str(exc) else exc.__class__.__name__)


# ---------------------------------------------------------------------------
# 响应宽容解析（信封字段下钻）
# ---------------------------------------------------------------------------


def unwrap_scopes(data) -> list:
    """沿信封包裹键递归下钻，返回 `[最外层, …, 最内层业务对象]`（限深）。"""
    scopes: list = []

    def walk(node, depth: int) -> None:
        if not isinstance(node, dict) or depth > UNWRAP_MAX_DEPTH:
            return
        scopes.append(node)
        for key in PAYLOAD_WRAPPER_KEYS:
            child = node.get(key)
            if isinstance(child, dict):
                walk(child, depth + 1)

    walk(data, 0)
    return scopes


def find_payload_field(data, key: str):
    """在各层 scope 查找 key（外层优先，保持顶层字段口径）。"""
    for scope in unwrap_scopes(data):
        if key in scope:
            return scope[key]
    return None


def as_int_tolerant(value):
    """宽容数值归一：int / 整值 float / 纯数字串 → int；其余 → None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text and text.isdigit():
            try:
                return int(text)
            except ValueError:
                return None
    return None


def parse_claim_reward(data) -> int | None:
    """从 claim 响应中提取本次签到奖励值。

    候选字段按「内层业务对象优先于顶层」在各信封层查找，仅接受正整数型数值；
    0 值占位字段（奖励未发放时的 `credits: 0`）跳过并继续向后查找。
    """
    if not isinstance(data, dict):
        return None
    for scope in reversed(unwrap_scopes(data)):
        for key in CLAIM_REWARD_KEYS:
            number = as_int_tolerant(scope.get(key))
            if number is not None and number > 0:
                return number
    return None


def resolve_claim_credits(claim_data, credits_before, recheck=None) -> tuple:
    """签到成功后解析 `(final_credits, final_delta, source)`。

    层1 claim 响应奖励字段优先；层2 复查 status 取当前余额，delta = 复查 - 签到前；
    层3 保底：delta = credits_before、final_credits = credits_before。
    """
    reward = parse_claim_reward(claim_data)
    if reward is not None:
        return (credits_before, reward, "claim_reward")

    if credits_before is not None and recheck is not None:
        ok_r, credits_after = recheck()
        if ok_r and credits_after is not None and credits_after - credits_before >= 0:
            return (credits_after, credits_after - credits_before, "balance_diff")

    if credits_before is not None:
        return (credits_before, credits_before, "legacy")
    return (None, 0, "legacy")


def humanize_device_limit(message: str) -> str:
    """识别「设备绑定数已达上限」类服务端错误并转译为可操作引导。

    服务端按 device_id 统计账号绑定数，超限后该设备上新登录的账号能换到 token
    但使用类接口（签到/积分查询）全拒。命中特征词时在原文后追加解决路径。
    """
    text = message or ""
    lower = text.lower()
    hit = (
        "绑定的账户数量已达上限" in text
        or "device limit" in lower
        or "bound to this device" in lower
    )
    if not hit:
        return text
    return (
        f"{text} —— 设备绑定数已达服务端上限：对本账号重新执行一次登录即可自动轮换到"
        "其他设备凭证，或在 Trae 客户端解绑不再使用的账号"
    )


# ---------------------------------------------------------------------------
# 错误分类 / 冷却
# ---------------------------------------------------------------------------


def classify_error(http_status: int, code) -> tuple[str, int]:
    """根据 HTTP 状态码和业务码分类签到错误，返回 `(error_type, cooldown_seconds)`。

    `cooldown_seconds`: -1=永久, 0=不冷却（仅记录错误计数）, >0=冷却秒数
    """
    if http_status == 200 and code == 1005:
        return ("PlanLimit", 43_200)
    if http_status == 429:
        return ("SoftRate", 60)
    if http_status == 401:
        return ("SessionDead", -1)
    if http_status == 404:
        return ("NotFound", 60)
    if 500 <= http_status < 600:
        return ("Server", 600)
    if 400 <= http_status < 500:
        return ("Client", 600)
    if code is not None and code != 0:
        return ("BusinessError", 300)
    return ("Unknown", 0)


def save_cooldown(uid: str, error_type: str, cooldown_seconds: int, reason: str) -> None:
    """写入/更新账号冷却状态。

    Server/Client 类错误前 2 次仅计数不冷却（until=0），第 3 次起进入冷却。
    `cooldown_seconds == 0` 表示清除冷却。
    """
    cooldowns = store.load_trae_cooldowns()
    if cooldown_seconds == 0:
        cooldowns.pop(uid, None)
        store.save_trae_cooldowns(cooldowns)
        return

    if cooldown_seconds == -1:
        entry = {
            "error_type": error_type,
            "until": FOREVER,
            "reason": reason,
            "error_count": 0,
        }
    elif error_type in ("Server", "Client"):
        previous = cooldowns.get(uid) or {}
        count = int(previous.get("error_count") or 0) + 1
        if count < 3:
            entry = {
                "error_type": error_type,
                "until": 0,
                "reason": reason,
                "error_count": count,
            }
        else:
            entry = {
                "error_type": error_type,
                "until": int(time.time()) + cooldown_seconds,
                "reason": reason,
                "error_count": 0,
            }
    else:
        entry = {
            "error_type": error_type,
            "until": int(time.time()) + cooldown_seconds,
            "reason": reason,
            "error_count": 0,
        }

    cooldowns[uid] = entry
    store.save_trae_cooldowns(cooldowns)


def clear_cooldown(uid: str) -> None:
    save_cooldown(uid, "", 0, "")


def cooldown_active(uid: str) -> str | None:
    """该账号是否处于冷却中；返回错误类型或 None。"""
    entry = store.load_trae_cooldowns().get(uid)
    if not isinstance(entry, dict):
        return None
    until = entry.get("until")
    if isinstance(until, (int, float)) and until > 0:
        if until >= FOREVER or until > time.time():
            return entry.get("error_type") or "Unknown"
    return None


def cooldown_until(uid: str) -> int:
    entry = store.load_trae_cooldowns().get(uid)
    if isinstance(entry, dict):
        until = entry.get("until")
        if isinstance(until, (int, float)):
            return int(until)
    return 0


# ---------------------------------------------------------------------------
# status 预检 / claim
# ---------------------------------------------------------------------------


def status_check(jwt: str, uid: str | None) -> dict:
    """预检今日是否已签。uid 解析失败直接报错（不发请求）。"""
    none = lambda message: {"ok": False, "checked_in": None, "credits": None, "message": message}
    if not uid:
        return none("无法从 JWT 解析 user id")

    device = get_device_for(uid)
    status, body, raw = http_post(jwt, device, STATUS_URL)
    if status == 0:
        return none(raw if raw else "网络异常")
    if not isinstance(body, dict):
        head = raw[:200]
        return none(f"非 JSON 响应: {head}")

    code = as_int_tolerant(body.get("code"))
    # 宽容解析：checked_in / credits 可能被 data 信封包裹；code 保持顶层口径。
    checked_raw = find_payload_field(body, "checked_in")
    if isinstance(checked_raw, bool):
        checked_in = checked_raw
    elif checked_raw is not None:
        number = as_int_tolerant(checked_raw)
        checked_in = None if number is None else number != 0
    else:
        checked_in = None
    credits = as_int_tolerant(find_payload_field(body, "credits"))
    message = body.get("message")
    message = message if isinstance(message, str) else ""

    if code != 0:
        return none(humanize_device_limit(message) if message else f"HTTP {status}")
    return {"ok": True, "checked_in": checked_in, "credits": credits, "message": message}


def signin_request(jwt: str, uid: str | None) -> tuple:
    """底层签到请求 + 响应解析 → `(ok, msg, code, http_status, data)`。

    `code=None` 同时是传输层异常标记（据此重试；非 JSON 响应不把 HTTP 状态码
    塞进业务 code，防 classify_error 误判）。
    """
    if not uid:
        return (False, "无法从 JWT 解析 user id", None, 0, None)

    device = get_device_for(uid)
    status, body, raw = http_post(jwt, device, SIGNIN_URL)
    if status == 0:
        return (False, raw if raw else "网络异常", None, 0, None)

    if isinstance(body, dict):
        code = as_int_tolerant(body.get("code"))
        message = body.get("message")
        if isinstance(message, str):
            msg = humanize_device_limit(message)
        else:
            msg = f"HTTP {status}"
        return (code == 0, msg, code, status, body)

    head = raw[:200]
    return (False, f"HTTP {status}: 非 JSON 响应: {head}", None, status, None)


def signin_with_retry(jwt: str, uid: str | None, retry: int) -> tuple:
    """签到（含网络异常重试）：仅传输层异常重试，业务失败不重试。"""
    last = (False, "无重试", None, 0, None)
    for attempt in range(retry + 1):
        last = signin_request(jwt, uid)
        if last[0] or last[2] is not None:
            return last
        if attempt < retry:
            time.sleep(RETRY_GAP)
    return last


# ---------------------------------------------------------------------------
# 侧写落盘
# ---------------------------------------------------------------------------


def save_credits_history(uid: str, credits: int, delta: int) -> None:
    """把账号最新积分与本次新增写入积分历史（按日期追加，90 天滚动）。"""
    store.append_credits_history(
        [
            {
                "date": util.local_date_str(),
                "platform": PLATFORM,
                "user_id": uid,
                "credits": credits,
                "delta": delta,
            }
        ]
    )


def save_summary_merged(results: list, warnings: list) -> None:
    """保存签到结果摘要（同日合并）。

    本轮未覆盖的账号（被跳过的已签账号）沿用当日旧记录，避免第二轮整份覆盖丢失
    「今日已签」状态。去重键 `user_id` 优先；旧记录无 user_id 时回退按 name 匹配。
    """
    today = util.local_date_str()
    merged = list(results)
    old = store.load_checkin_summary()
    old_time = old.get("time") if isinstance(old.get("time"), str) else ""
    old_results = old.get("results")
    if isinstance(old_results, list) and old_time.startswith(today):
        new_uids = {
            str(row.get("user_id"))
            for row in merged
            if str(row.get("user_id") or "").strip()
        }
        new_names = {str(row.get("name")) for row in merged}
        for row in old_results:
            if not isinstance(row, dict):
                continue
            uid = str(row.get("user_id") or "")
            name = str(row.get("name") or "")
            superseded = uid in new_uids if uid else name in new_names
            if not superseded:
                merged.append(row)

    total_ok = sum(1 for row in merged if row.get("ok") is True)
    already = sum(1 for row in merged if row.get("action") == "skip_already")
    failed = sum(1 for row in merged if row.get("ok") is not True)
    store.save_checkin_summary(
        {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "results": merged,
            "total_ok": total_ok,
            "already": already,
            "failed": failed,
            "warnings": warnings,
        }
    )


def _append_log_line(line: str) -> None:
    store.app_log(line)


# ---------------------------------------------------------------------------
# 单轮主流程
# ---------------------------------------------------------------------------


def run_round(accounts: list, retry: int = 1, gap_secs: int = 3, emit=None) -> dict:
    """执行一轮签到：逐账号串行处理，返回汇总 `{"ok", "already", "failed", "rows"}`。

    单账号失败不中断整轮；侧写全部落盘（冷却 / 积分历史 / 摘要 / 日志）。
    `emit` 为可选回调，逐条接收进度事件 dict。
    """
    def _emit(event: dict) -> None:
        if emit is not None:
            try:
                emit(event)
            except Exception:
                pass

    rows: list = []
    warnings: list = []
    total_ok = 0
    already = 0
    failed = 0

    _emit({"type": "start", "total": len(accounts)})

    for index, acc in enumerate(accounts):
        if index > 0 and gap_secs > 0:
            time.sleep(gap_secs)

        # JWT 已过期/临期且带 refresh_token 时先自动续一次（失败保持原状，
        # 签到会按 SessionDead 分类进冷却，不会无限重试）
        acc = ensure_fresh_trae_jwt(acc)

        idx = index + 1
        name = acc.get("name") or acc.get("nickname") or f"账号{idx}"
        jwt = str(acc.get("jwt") or "").strip()

        if not jwt:
            failed += 1
            uid = acc.get("user_id") or ""
            rows.append(
                {
                    "index": idx,
                    "user_id": uid,
                    "name": name,
                    "status": "fail",
                    "message": "未配置 jwt",
                }
            )
            _emit(
                {
                    "type": "account",
                    "index": idx,
                    "user_id": uid,
                    "name": name,
                    "status": "fail",
                    "message": "未配置 jwt",
                }
            )
            continue

        info = jwt_parse(jwt)
        uid_str = info["user_id"] or ""

        # JWT 过期告警（只影响续期提醒，不阻塞签到）
        if info["exp_hours"] is not None and info["exp_timestamp"] is not None:
            exp_txt = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(info["exp_timestamp"])
            )
            if info["exp_hours"] < 0:
                warnings.append(f"{name}: JWT 已过期({exp_txt})，请重新抓取")
            elif info["exp_hours"] < EXPIRY_WARN_HOURS:
                warnings.append(
                    f"{name}: JWT 将于 {info['exp_hours']:.1f}h 后过期({exp_txt})，请重新抓取"
                )

        # status 预检：已签跳过 claim
        pre = status_check(jwt, info["user_id"])
        if pre["ok"] and pre["checked_in"] is True:
            already += 1
            row = {
                "index": idx,
                "user_id": info["user_id"],
                "name": name,
                "status": "already",
                "credits": pre["credits"],
                "message": pre["message"] or "已签到",
            }
            rows.append(row)
            _emit(dict(row, type="account"))
            if pre["credits"] is not None:
                save_credits_history(uid_str, pre["credits"], 0)
            continue

        ok, msg, code, http_status, claim_data = signin_with_retry(jwt, info["user_id"], retry)
        final_credits = None
        final_delta = 0
        error_type = None
        cooldown_until_value = None

        if ok:
            clear_cooldown(uid_str)

            def recheck():
                result = status_check(jwt, info["user_id"])
                return (result["ok"], result["credits"])

            credits, delta, _src = resolve_claim_credits(claim_data, pre["credits"], recheck)
            final_credits = credits
            final_delta = delta
            if final_credits is not None:
                save_credits_history(uid_str, final_credits, final_delta)
            total_ok += 1
        else:
            error_type, cooldown_secs = classify_error(http_status, code)
            if error_type != "Unknown":
                save_cooldown(uid_str, error_type, cooldown_secs, msg)
                cooldown_until_value = cooldown_until(uid_str)
            failed += 1

        row = {
            "index": idx,
            "user_id": info["user_id"],
            "name": name,
            "status": "success" if ok else "fail",
            "code": code,
            "message": msg,
            "credits": final_credits,
            "delta": final_delta or None,
            "error_type": error_type,
            "cooldown_until": cooldown_until_value,
        }
        rows.append(row)
        _emit(dict(row, type="account"))

    _append_log_line(
        f"[Trae 签到] 成功{total_ok}/已签到{already}/失败{failed}/总计{len(accounts)}"
    )
    if warnings:
        _append_log_line(f"[Trae 签到] 告警: {'; '.join(warnings)}")

    summary_rows = [
        {
            "name": row.get("name"),
            "user_id": row.get("user_id"),
            "ok": row.get("status") in ("success", "already"),
            "action": "skip_already" if row.get("status") == "already" else "claim",
            "credits": row.get("credits"),
            "message": row.get("message"),
        }
        for row in rows
    ]
    save_summary_merged(summary_rows, warnings)

    done = {"type": "done", "ok": total_ok, "already": already, "failed": failed}
    _emit(done)
    return {"ok": total_ok, "already": already, "failed": failed, "rows": rows, "warnings": warnings}


def allowed_accounts(cfg: dict | None = None) -> list:
    """参与自动签到的账号（排除被排除项与冷却中账号）。"""
    cfg = cfg if cfg is not None else store.load_config()
    section = store.platform_config(PLATFORM, cfg)
    excluded = section.get("excluded_account_ids") or []
    result = []
    for acc in store.load_accounts(PLATFORM):
        uid = account_uid(acc)
        if acc.get("id") in excluded or uid in excluded:
            continue
        if uid and cooldown_active(uid):
            continue
        result.append(acc)
    return result


def account_uid(account: dict) -> str:
    """账号的 Trae user id：优先已有字段，其次从 JWT 解析。"""
    uid = account.get("user_id")
    if isinstance(uid, str) and uid:
        return uid
    jwt = str(account.get("jwt") or "").strip()
    if jwt:
        return jwt_parse(jwt)["user_id"] or ""
    return ""


def run_checkin_cycle(emit=None) -> dict:
    """Trae 一轮自动签到（后台调度调用）。

    含失败重试：最多 2 轮，间隔 30s / 90s，仅重试失败账号，且排除 SessionDead
    （JWT 被吊销，重试必然再 401）这类永久失效账号。
    """
    cfg = store.load_config()
    section = store.platform_config(PLATFORM, cfg)
    if cfg.get("enabled") is not True or section.get("enabled") is not True:
        return {"status": "disabled"}

    accounts = allowed_accounts(cfg)
    if not accounts:
        return {"status": "no_accounts"}

    retry = int(section.get("retry") or 1)
    gap = int(section.get("checkin_gap_secs") or 3)
    outcome = run_round(accounts, retry=retry, gap_secs=gap, emit=emit)
    rows = outcome["rows"]

    final_status = {
        str(row.get("user_id") or ""): row.get("status")
        for row in rows
        if row.get("user_id")
    }
    error_types = {
        str(row.get("user_id") or ""): row.get("error_type")
        for row in rows
        if row.get("user_id") and row.get("error_type")
    }

    for delay in (30, 90):
        failed_uids = [
            uid
            for uid, status in final_status.items()
            if status == "fail" and error_types.get(uid) != "SessionDead"
        ]
        if not failed_uids:
            break
        store.app_log(f"[Trae 签到] 重试轮: {len(failed_uids)} 个失败账号，{delay}s 后开始")
        if emit is not None:
            try:
                emit({"type": "retry", "delay": delay, "total": len(failed_uids)})
            except Exception:
                pass
        time.sleep(delay)
        retry_accounts = [
            acc for acc in allowed_accounts(cfg) if account_uid(acc) in failed_uids
        ]
        if not retry_accounts:
            break
        round_outcome = run_round(retry_accounts, retry=retry, gap_secs=gap, emit=emit)
        for row in round_outcome["rows"]:
            uid = str(row.get("user_id") or "")
            if not uid:
                continue
            final_status[uid] = row.get("status")
            if row.get("status") != "fail":
                error_types.pop(uid, None)
            elif row.get("error_type"):
                error_types[uid] = row.get("error_type")

    ok = sum(1 for status in final_status.values() if status == "success")
    already_count = sum(1 for status in final_status.values() if status == "already")
    failed_count = sum(1 for status in final_status.values() if status == "fail")

    # 逐账号明细（含重试后的最终状态）：前端签到卡片与 WB 侧共用同一渲染
    name_by_uid: dict[str, str] = {}
    msg_by_uid: dict[str, str] = {}
    for row in rows:
        uid = str(row.get("user_id") or "")
        if not uid:
            continue
        name_by_uid.setdefault(uid, str(row.get("name") or uid))
        if row.get("message"):
            msg_by_uid[uid] = str(row.get("message"))
    detail_rows = [
        {
            "name": name_by_uid.get(uid, uid),
            "status": _map_status(status),
            "message": msg_by_uid.get(uid, ""),
        }
        for uid, status in sorted(final_status.items())
    ]

    # 签到结果按日落库，供前端「今日状态」与趋势展示
    for uid, status in final_status.items():
        store.add_checkin_log(
            {
                "ts": util.now_ms(),
                "platform": PLATFORM,
                "accountId": uid,
                "email": uid,
                "result": _map_status(status),
            }
        )

    store.app_log(
        f"[Trae 签到] 结束: 成功 {ok} · 已签 {already_count} · 失败 {failed_count} · "
        f"总计 {len(final_status)}"
    )
    return {
        "status": "ok",
        "ok": ok,
        "already": already_count,
        "failed": failed_count,
        "total": len(final_status),
        "rows": detail_rows,
    }


def _map_status(status: str) -> str:
    if status == "success":
        return "success"
    if status == "already":
        return "already"
    return "error"
