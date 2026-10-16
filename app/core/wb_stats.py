#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 计费统计：积分余额 / 积分包 / 官方请求用量。

两个域必须分开，混用会直接被网关拒：

- **积分资源**（余额、积分包）请求账号自身域：国内版 `www.codebuddy.cn`
  （账号 `domain` 为 workbuddy.cn 时改走 `www.workbuddy.cn`），国际版
  `www.workbuddy.ai`；
- **官方请求用量**固定走 `www.workbuddy.cn`（国际版 `www.workbuddy.ai`），
  该接口不跟随 codebuddy.cn。

请求一律浏览器形态：Bearer 鉴权 + `X-Client-Platform: web` + `Origin`/`Referer`
指向本次请求域。缺这些头会被网关当成未知客户端（10085 / 一致性校验失败）。

接口（均 POST JSON）：

- `/billing/meter/get-user-resource-summary`        资源总览（`data.Packages[]`，无到期时间）
- `/billing/meter/get-user-resource-paid-packages`  付费包明细（`data.Accounts[]`，含到期时间）
- `/billing/meter/get-user-resource-free-packages`  免费包明细（`data.Accounts[]`）
- `/billing/meter/get-user-resource`                单接口形态（国际版，`data.Accounts[]`）
- `/billing/meter/get-user-request-usage`           请求级用量（`data.data[]`，分页）

官方字段名版本差异大（剩余量三种命名并存：`CycleCapacityRemainPrecise` /
`CycleRemainCapacity` / `CapacityRemain`），解析一律走候选键链。

**用量必须按时间推进连续扫描**：服务端单次最多回 [`USAGE_PAGE_SIZE`] 条并
把 `total` 一起截断成同一数字，翻页拿不到更新记录 —— 只能「取满即把起点
推进到本轮最新一条」，反复扫到某轮不满为止。

结果做进程内缓存（10 分钟），面板高频刷新不会打满计费域。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from . import store, util, wb_accounts, wb_checkin
from . import variant as wb_variant

# 积分资源域
CN_BILLING_BASE = "https://www.codebuddy.cn"
CN_ALT_BILLING_BASE = "https://www.workbuddy.cn"
AI_BILLING_BASE = "https://www.workbuddy.ai"

# 官方请求用量域（固定 workbuddy.cn / workbuddy.ai，不跟随账号 domain）
USAGE_PATH = "/billing/meter/get-user-request-usage"
USAGE_PAGE_SIZE = 3000
USAGE_MAX_ROUNDS = 60

CACHE_TTL_SECONDS = 600
_CACHE: dict[tuple, tuple[float, object]] = {}

# 积分包查询码表：官方套餐码集（请求体多带不存在的码无副作用）。
PAID_PACKAGE_CODES = (
    "TCACA_code_002_AkiJS3ZHF5",
    "TCACA_code_023_4xbGhMrE6q",
    "TCACA_code_026_BaESVICNoi",
    "TCACA_code_027_0FCGVA6vSa",
    "TCACA_code_009_0XmEQc2xOf",
    "TCACA_code_038_OhvqZtiPKr",
    "TCACA_code_003_FAnt7lcmRT",
    "TCACA_code_036_lupO5WgNdG",
)
FREE_PACKAGE_CODES = (
    "TCACA_code_008_cfWoLwvjU4",
    "TCACA_code_007_nzdH5h4Nl0",
    "TCACA_code_028_NtpWi0jzXs",
    "TCACA_code_029_6wCGEWquYy",
    "TCACA_code_030_BjSt89qTvr",
    "TCACA_code_001_PqouKr6QWV",
    "TCACA_code_006_DbXS0lrypC",
    "TCACA_code_035_ArVxJcGDsm",
    "TCACA_code_037_WxOD3MpI2o",
    "TCACA_code_039_KRcQj7wUat",
    "TCACA_code_040_mi9rCYg46x",
)
PRODUCT_CODE = "p_tcaca"

# 到期时间判定：DeductionEndTime 比 CycleEndTime 晚超过该天数时视为长期占位，
# 改用周期结束时间；最终值距 now 超过 FAR_FUTURE 天数视作长期有效（不展示）。
EXPIRY_CYCLE_OVERRIDE_DAYS = 365
FAR_FUTURE_EXPIRY_DAYS = 730
EXPIRING_SOON_DAYS = 7

# 容量字段候选键链（总览 / 明细 / 切片三层命名并存）
TOTAL_KEYS = (
    "CycleCapacitySizePrecise",
    "CycleCapacitySize",
    "CycleTotalCapacity",
    "CapacitySizePrecise",
    "CapacitySize",
    "SlicePeriodCapacitySizePrecise",
    "SlicePeriodCapacitySize",
)
REMAIN_KEYS = (
    "CycleCapacityRemainPrecise",
    "CycleCapacityRemain",
    "CycleRemainCapacity",
    "CapacityRemainPrecise",
    "CapacityRemain",
    "SlicePeriodCapacityRemainPrecise",
    "SlicePeriodCapacityRemain",
)
USED_KEYS = (
    "CycleCapacityUsedPrecise",
    "CycleCapacityUsed",
    "CycleUsedCapacity",
    "CapacityUsedPrecise",
    "CapacityUsed",
    "SlicePeriodCapacityUsedPrecise",
    "SlicePeriodCapacityUsed",
)
EXPIRE_KEYS = ("DeductionEndTime", "deductionEndTime", "ExpiredTime", "expiredTime", "expireTime")
CYCLE_KEYS = ("CycleEndTime", "cycleEndTime")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def billing_base(account: dict) -> str:
    """积分资源请求基址（跟账号档位与 domain 走）。"""
    if wb_variant.from_account(account) == wb_variant.AI:
        return AI_BILLING_BASE
    domain = str(account.get("domain") or "").strip().lower()
    if domain in ("workbuddy.cn", "www.workbuddy.cn"):
        return CN_ALT_BILLING_BASE
    return CN_BILLING_BASE


def usage_base(account: dict) -> str:
    """官方请求用量基址（固定域，不跟随 codebuddy.cn）。"""
    return AI_BILLING_BASE if wb_variant.from_account(account) == wb_variant.AI else CN_ALT_BILLING_BASE


def _headers(account: dict, origin: str) -> dict:
    headers = wb_accounts.build_auth_headers(account)
    headers["Accept"] = "application/json, text/plain, */*"
    headers["X-Client-Platform"] = "web"
    headers["Origin"] = origin
    headers["Referer"] = f"{origin}/profile/plans-usage"
    return headers


def _post(account: dict, base: str, path: str, body: dict) -> dict:
    """计费域请求：401 时刷新一次重试。返回原始响应 dict。"""
    url = base + path
    resp = util.http_json(url, "POST", body=body, headers=_headers(account, base))
    if wb_checkin.is_unauthorized(resp) and str(account.get("refresh_token") or "").strip():
        refreshed = wb_accounts.refresh_account_token(account)
        resp = util.http_json(url, "POST", body=body, headers=_headers(refreshed, base))
    return resp


def _code_of(resp: dict):
    """响应业务码：顶层 code 优先，其次 data.code（两种包装官方都在用）。"""
    if not isinstance(resp, dict):
        return None
    raw = resp.get("code")
    if raw is None and isinstance(resp.get("data"), dict):
        raw = resp["data"].get("code")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _is_route_missing(resp: dict) -> bool:
    return _code_of(resp) == 404


def _is_success(resp: dict) -> bool:
    """成功判定：有 code 时必须 0/200；无 code 时要求带 data 且未被显式标失败。"""
    if not isinstance(resp, dict):
        return False
    code = _code_of(resp)
    if code is not None:
        return code in (0, 200)
    if resp.get("ok") is False or resp.get("success") is False:
        return False
    return resp.get("data") is not None


def _post_paths(account: dict, base: str, paths: tuple[str, ...], body: dict) -> dict:
    """候选路径依次尝试，**只有 404 才回落**（401/10085/传输错误原样返回，避免掩盖真因）。"""
    resp: dict = {}
    for index, path in enumerate(paths):
        resp = _post(account, base, path, body)
        if index + 1 == len(paths) or not _is_route_missing(resp):
            return resp
    return resp


def _error_text(resp: dict, fallback: str) -> str:
    code = _code_of(resp)
    message = ""
    if isinstance(resp, dict):
        message = str(resp.get("message") or resp.get("msg") or "")
        if not message and isinstance(resp.get("data"), dict):
            message = str(resp["data"].get("message") or resp["data"].get("msg") or "")
    message = message.strip()[:120]
    if message:
        return f"{fallback}（code={code}）：{message}" if code is not None else f"{fallback}：{message}"
    return f"{fallback}（code={code}）" if code is not None else fallback


def _num(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _first(node: dict, keys: tuple) -> object:
    for key in keys:
        value = node.get(key)
        if value is not None and value != "":
            return value
    return None


def _parse_ts(value) -> int | None:
    """时间归一为毫秒：秒/毫秒数字（含字符串数字）、RFC3339、本地时间串、纯日期。"""
    ts = util.norm_ts(value)
    if ts:
        return ts
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).timestamp() * 1000)
        except ValueError:
            continue
    try:
        day = datetime.strptime(text, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        return None
    return int(day.timestamp() * 1000)


# 请求行里的 Token 维度键名随官方版本变化，这里只认「键名里带 token」的顶层数值字段，
# 按语义归类；上游不返回时一律为 None（前端据此显示「官方未提供 Token 维度」）。
_TOKEN_INPUT_HINTS = ("input", "prompt")
_TOKEN_OUTPUT_HINTS = ("output", "completion")


def _token_fields(item: dict) -> dict:
    out = {"input": None, "output": None, "cacheRead": None, "cacheWrite": None}
    for key, value in item.items():
        name = str(key).lower()
        if "token" not in name:
            continue
        num = _num(value)
        if num is None:
            continue
        if "cache" in name and "read" in name:
            out["cacheRead"] = num
        elif "cache" in name and ("write" in name or "creation" in name):
            out["cacheWrite"] = num
        elif any(hint in name for hint in _TOKEN_OUTPUT_HINTS):
            out["output"] = num
        elif any(hint in name for hint in _TOKEN_INPUT_HINTS):
            out["input"] = num
    return out


def _cached(key: tuple, producer, force: bool = False):
    """10 分钟进程内缓存：统计类接口外部请求多，面板高频刷新不能每次都打满。"""
    hit = _CACHE.get(key)
    now = time.time()
    if not force and hit and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    data = producer()
    if isinstance(data, dict) and data.get("ok"):
        _CACHE[key] = (now, data)
    return data


def peek_all_credits():
    """只读缓存里的全账号积分快照；未命中返回 None。

    首屏聚合接口用它取数：统计要打外部网络，绝不能落在首屏同步路径上。
    """
    return _peek(("wb-credits-all", "*"))


def peek_all_usage(days: int = 31):
    """只读缓存里指定天数的全账号用量快照；未命中返回 None。"""
    return _peek(("wb-usage-all", "*", int(days)))


def _peek(key: tuple):
    """只读缓存：命中且未过期才返回，否则 None —— 绝不触发外部请求。"""
    hit = _CACHE.get(key)
    if not hit:
        return None
    if time.time() - hit[0] >= CACHE_TTL_SECONDS:
        return None
    return hit[1]


def stats_invalidate(account_id: str | None = None) -> None:
    """账号变更（增删/刷新 token）后失效缓存。"""
    del account_id
    _CACHE.clear()


# ---------------------------------------------------------------------------
# 积分余额与积分包
# ---------------------------------------------------------------------------

# 官方把资源塞在不同包装层里，逐层探测（键名大小写不敏感）
_CONTAINER_PATHS = (
    (),
    ("data",),
    ("data", "data"),
    ("data", "Response", "Data"),
    ("data", "data", "Response", "Data"),
)


def _find_array(resp: dict, *names: str) -> list | None:
    """在响应各层包装里找目标数组；找不到返回 None（与「空数组」语义区分）。"""
    if not isinstance(resp, dict):
        return None
    wanted = {name.lower() for name in names}
    for path in _CONTAINER_PATHS:
        node: object = resp
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                node = None
                break
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if str(key).lower() in wanted and isinstance(value, list):
                return value
    return None


def _slice_nodes(item: dict) -> list[dict]:
    """资源条目本身 + 其切片明细首条（部分形态容量只写在切片里）。"""
    nodes = [item]
    for key, value in item.items():
        if str(key).lower() == "sliceperiodusagedetails" and isinstance(value, list):
            nodes.extend(entry for entry in value if isinstance(entry, dict))
            break
    return nodes


def _pick_num(nodes: list[dict], keys: tuple) -> float | None:
    for node in nodes:
        value = _num(_first(node, keys))
        if value is not None:
            return value
    return None


def _resolve_expire(nodes: list[dict], now_ms: int) -> int | None:
    """到期时间：优先 DeductionEndTime/ExpiredTime；若 CycleEndTime 早其超过一年，
    视前者为长期占位改用周期结束；最终值超过 FAR_FUTURE 天视为长期有效。"""
    deduction = cycle = None
    for node in nodes:
        if deduction is None:
            deduction = _parse_ts(_first(node, EXPIRE_KEYS))
        if cycle is None:
            cycle = _parse_ts(_first(node, CYCLE_KEYS))
    override_ms = EXPIRY_CYCLE_OVERRIDE_DAYS * 86400_000
    if deduction and cycle and deduction - cycle > override_ms:
        expire = cycle
    else:
        expire = deduction or cycle
    if expire and expire - now_ms > FAR_FUTURE_EXPIRY_DAYS * 86400_000:
        return None
    return expire


def _resource_row(item: dict, kind: str, now_ms: int) -> dict:
    """单条资源归一：官方同一份数据有多种字段命名，逐级兜底。"""
    nodes = _slice_nodes(item)
    total = _pick_num(nodes, TOTAL_KEYS)
    remaining = _pick_num(nodes, REMAIN_KEYS)
    used = _pick_num(nodes, USED_KEYS)
    if total is None:
        if remaining is not None and used is not None:
            total = remaining + used
        else:
            total = remaining if remaining is not None else used
    if remaining is None:
        remaining = max(0.0, (total or 0.0) - (used or 0.0))
    if used is None:
        used = max(0.0, (total or 0.0) - remaining)
    total = max(0.0, total or 0.0)
    remaining = max(0.0, remaining)
    used = max(0.0, used)
    expire_ts = _resolve_expire(nodes, now_ms)
    name = _first(item, ("PackageName", "packageName", "name", "Code", "code"))
    code = _first(item, ("PackageCode", "packageCode"))
    return {
        "name": str(name or "积分包"),
        "code": str(code or ""),
        "kind": kind,
        "total": round(total, 4),
        "remaining": round(remaining, 4),
        "used": round(used, 4),
        "expireTs": expire_ts,
        "expireText": datetime.fromtimestamp(expire_ts / 1000).strftime("%m/%d %H:%M") if expire_ts else "",
    }


def _merge_resources(summary_rows: list[dict], detail_rows: list[dict]) -> list[dict]:
    """明细优先：总览里与明细同码的包丢弃（明细带到期时间），其余保留。"""
    detail_codes = {row["code"] for row in detail_rows if row["code"]}
    merged = list(detail_rows)
    merged.extend(row for row in summary_rows if not row["code"] or row["code"] not in detail_codes)
    return merged


def _paid_body() -> dict:
    return {
        "PageNumber": 1,
        "PageSize": 200,
        "Status": [0, 3],
        "PackageCodes": list(PAID_PACKAGE_CODES),
        "NeedRenewInfo": True,
    }


def _free_body() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "PageNumber": 1,
        "PageSize": 200,
        "Status": [0, 3],
        "SlicePeriodStartTime": f"{today} 00:00:00",
        "SlicePeriodEndTime": f"{today} 23:59:59",
        "PackageCodes": list(FREE_PACKAGE_CODES),
    }


def _legacy_body() -> dict:
    now = datetime.now()
    end = now + timedelta(days=365 * 101)
    return {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": PRODUCT_CODE,
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": now.strftime("%Y-%m-%d %H:%M:%S"),
        "PackageEndTimeRangeEnd": end.strftime("%Y-%m-%d %H:%M:%S"),
    }


_LEGACY_PATHS = ("/billing/meter/get-user-resource", "/v2/billing/meter/get-user-resource")


def _failed(account: dict, error: str) -> dict:
    return {
        "ok": False,
        "id": account.get("id"),
        "uid": str(account.get("uid") or account.get("id") or ""),
        "name": wb_accounts.display_name(account),
        "variant": wb_variant.from_account(account),
        "error": error,
    }


def fetch_credits(account: dict, force: bool = False) -> dict:
    """单账号积分余额 + 积分包列表。失败返回 `{"ok": False, "error"}`。"""

    def produce() -> dict:
        now_ms = util.now_ms()
        base = billing_base(account)
        variant = wb_variant.from_account(account)

        if variant == wb_variant.AI:
            rows = _legacy_rows(account, base, "资源")
            if isinstance(rows, str):
                return _failed(account, rows)
        else:
            summary_resp = _post_paths(
                account,
                base,
                ("/billing/meter/get-user-resource-summary", "/v2/billing/meter/get-user-resource-summary"),
                {},
            )
            errors: list[str] = []
            summary_rows: list[dict] = []
            summary_items = _find_array(summary_resp, "Packages", "packages")
            if _is_success(summary_resp) and summary_items is not None:
                summary_rows = [_resource_row(i, "总览", now_ms) for i in summary_items if isinstance(i, dict)]
            else:
                errors.append(_error_text(summary_resp, "资源总览查询失败"))

            detail_rows: list[dict] = []
            probes = (
                ("paid", "付费", _paid_body()),
                ("free", "免费", _free_body()),
            )
            for suffix, kind, body in probes:
                resp = _post_paths(
                    account,
                    base,
                    (
                        f"/billing/meter/get-user-resource-{suffix}-packages",
                        f"/v2/billing/meter/get-user-resource-{suffix}-packages",
                    ),
                    body,
                )
                items = _find_array(resp, "Accounts", "accounts")
                if _is_success(resp) and items is not None:
                    detail_rows.extend(_resource_row(i, kind, now_ms) for i in items if isinstance(i, dict))
                else:
                    errors.append(_error_text(resp, f"{kind}积分包查询失败"))

            rows = _merge_resources(summary_rows, detail_rows)
            if not rows and len(errors) >= 3:
                # 三路全失败 → 单接口形态兜底一次
                legacy_rows = _legacy_rows(account, base, "资源")
                if isinstance(legacy_rows, str):
                    return _failed(account, errors[0])
                rows = legacy_rows

        rows.sort(key=lambda r: (r["expireTs"] or 9e15, -r["remaining"]))
        balance = round(sum(row["remaining"] for row in rows), 2)
        return {
            "ok": True,
            "id": account.get("id"),
            "uid": str(account.get("uid") or account.get("id") or ""),
            "name": wb_accounts.display_name(account),
            "variant": variant,
            "balance": balance,
            "packages": rows,
            "fetchedAt": now_ms,
        }

    return _cached(("wb-credits", str(account.get("id") or "")), produce, force)


def _legacy_rows(account: dict, base: str, kind: str) -> list[dict] | str:
    """单接口形态取资源；成功返回行列表，失败返回可读错误字符串。"""
    resp = _post_paths(account, base, _LEGACY_PATHS, _legacy_body())
    items = _find_array(resp, "Accounts", "accounts")
    if not (_is_success(resp) and items is not None):
        return _error_text(resp, "积分查询失败")
    now_ms = util.now_ms()
    return [_resource_row(i, kind, now_ms) for i in items if isinstance(i, dict)]


def fetch_all_credits(force: bool = False) -> dict:
    """全账号积分余额聚合（逐账号串行，失败账号不阻塞其余）。"""

    def produce() -> dict:
        accounts = store.load_accounts(store.PLATFORM_WORKBUDDY)
        rows = [fetch_credits(acc, force) for acc in accounts]
        ok_rows = [r for r in rows if r.get("ok")]
        total = sum(r["balance"] for r in ok_rows if r.get("balance") is not None)
        package_count = sum(len(r.get("packages") or []) for r in ok_rows)
        return {
            "ok": True,
            "accounts": rows,
            "totalBalance": round(total, 2),
            "packageCount": package_count,
            "fetchedAt": util.now_ms(),
        }

    return _cached(("wb-credits-all", "*"), produce, force)


# ---------------------------------------------------------------------------
# 官方请求用量（积分消耗明细）
# ---------------------------------------------------------------------------


def fetch_usage(account: dict, days: int = 31, force: bool = False) -> dict:
    """近 N 天请求级用量：逐日积分 + 按模型 + 会话预览 TopN。

    服务端单次只回一页且把 `total` 一起截断，故不靠 total 翻页，而是
    「取满一页就把起点推进到本轮最新一条的时间戳」连续扫描。
    """

    def produce() -> dict:
        base = usage_base(account)
        today = datetime.now().date()
        span = max(1, int(days))
        range_start = today - timedelta(days=span - 1)
        end_time = f"{today} 23:59:59"
        start_time = f"{range_start} 00:00:00"

        rows: list[dict] = []
        seen: set[tuple] = set()
        previous_newest: int | None = None
        rounds = 0
        while True:
            resp = _post(
                account,
                base,
                USAGE_PATH,
                {"startTime": start_time, "endTime": end_time, "pageNum": 1, "pageSize": USAGE_PAGE_SIZE},
            )
            if not _is_success(resp):
                return _failed(account, _error_text(resp, "官方用量查询失败"))
            data = resp.get("data")
            raw_items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(raw_items, list):
                return _failed(account, "官方响应格式无效")

            page_rows: list[dict] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                credit = _num(item.get("credit"))
                ts = _parse_ts(_first(item, ("requestTime", "request_time")))
                if credit is None or credit < 0 or not ts:
                    continue
                rid = str(item.get("requestId") or item.get("request_id") or "")
                key = (rid, ts)
                if rid and key in seen:
                    continue
                seen.add(key)
                tokens = _token_fields(item)
                page_rows.append(
                    {
                        "ts": ts,
                        "credit": credit,
                        "model": str(item.get("model") or "未知模型"),
                        "client": str(item.get("client") or ""),
                        "requestId": rid,
                        **tokens,
                    }
                )
            rows.extend(
                row for row in page_rows if range_start <= datetime.fromtimestamp(row["ts"] / 1000).date() <= today
            )

            if len(raw_items) < USAGE_PAGE_SIZE:
                break
            newest = max((row["ts"] for row in page_rows), default=None)
            if newest is None or (previous_newest is not None and newest <= previous_newest):
                break
            rounds += 1
            if rounds >= USAGE_MAX_ROUNDS:
                break
            start_time = datetime.fromtimestamp(newest / 1000).strftime("%Y-%m-%d %H:%M:%S")
            previous_newest = newest

        today_key = today.strftime("%Y-%m-%d")
        daily: dict[str, float] = {}
        daily_models: dict[str, dict[str, float]] = {}
        model_totals: dict[str, float] = {}
        model_calls: dict[str, int] = {}
        token_totals = {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0}
        token_seen = False
        for row in rows:
            date = datetime.fromtimestamp(row["ts"] / 1000).strftime("%Y-%m-%d")
            daily[date] = daily.get(date, 0.0) + row["credit"]
            daily_models.setdefault(date, {})
            daily_models[date][row["model"]] = daily_models[date].get(row["model"], 0.0) + row["credit"]
            model_totals[row["model"]] = model_totals.get(row["model"], 0.0) + row["credit"]
            model_calls[row["model"]] = model_calls.get(row["model"], 0) + 1
            for key in token_totals:
                value = row.get(key)
                if value is not None:
                    token_totals[key] += value
                    token_seen = True

        today_total = daily.get(today_key, 0.0)
        week_total = sum(v for k, v in daily.items() if (today - datetime.strptime(k, "%Y-%m-%d").date()).days < 7)
        month_total = sum(v for k, v in daily.items() if k.startswith(today.strftime("%Y-%m")))

        top_sessions = sorted(rows, key=lambda r: -r["credit"])[:10]
        models = sorted(
            ({"model": m, "credits": round(c, 2), "calls": model_calls.get(m, 0)} for m, c in model_totals.items()),
            key=lambda r: -r["credits"],
        )
        # 展示口径：输入已含缓存读，总量 = 输入 + 输出（不与缓存重复计）
        tokens_summary = {key: int(round(value)) for key, value in token_totals.items()}
        tokens_summary["total"] = tokens_summary["input"] + tokens_summary["output"]
        session_row = lambda r: {
            "ts": r["ts"],
            "credit": round(r["credit"], 2),
            "model": r["model"],
            "client": r["client"],
            "requestId": r["requestId"],
            "input": int(round(r["input"] or 0)),
            "output": int(round(r["output"] or 0)),
            "cacheRead": int(round(r["cacheRead"] or 0)),
            "cacheWrite": int(round(r["cacheWrite"] or 0)),
            "tokens": int(round((r["input"] or 0) + (r["output"] or 0))),
        }
        return {
            "ok": True,
            "id": account.get("id"),
            "name": wb_accounts.display_name(account),
            "variant": wb_variant.from_account(account),
            "days": span,
            "today": round(today_total, 2),
            "week": round(week_total, 2),
            "month": round(month_total, 2),
            "tokenAvailable": token_seen,
            "tokenTotals": tokens_summary if token_seen else None,
            "todayTokens": (
                {
                    "input": int(round(sum(r["input"] or 0 for r in rows if datetime.fromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d") == today_key))),
                    "output": int(round(sum(r["output"] or 0 for r in rows if datetime.fromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d") == today_key))),
                    "cacheRead": int(round(sum(r["cacheRead"] or 0 for r in rows if datetime.fromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d") == today_key))),
                }
                if token_seen
                else None
            ),
            "cacheHitRate": (
                (token_totals["cacheRead"] / token_totals["input"]) if token_seen and token_totals["input"] else None
            ),
            "daily": [
                {"date": k, "credits": round(v, 2), "models": daily_models.get(k, {})} for k, v in sorted(daily.items())
            ],
            "models": models,
            "sessions": [session_row(r) for r in top_sessions],
            "requests": [session_row(r) for r in sorted(rows, key=lambda r: -r["ts"])[:100]],
            "totalCalls": len(rows),
            "fetchedAt": util.now_ms(),
        }

    return _cached(("wb-usage", str(account.get("id") or ""), int(days)), produce, force)


def fetch_all_usage(days: int = 31, force: bool = False) -> dict:
    """全账号用量聚合（逐账号，含跨账号合计与按账号构成）。"""

    def produce() -> dict:
        accounts = store.load_accounts(store.PLATFORM_WORKBUDDY)
        per_account = [fetch_usage(acc, days, force) for acc in accounts]
        ok_rows = [r for r in per_account if r.get("ok")]
        daily: dict[str, dict] = {}
        model_totals: dict[str, float] = {}
        for row in ok_rows:
            for day in row["daily"]:
                slot = daily.setdefault(day["date"], {"credits": 0.0, "models": {}})
                slot["credits"] += day["credits"]
                for model, credit in day["models"].items():
                    slot["models"][model] = slot["models"].get(model, 0.0) + credit
                    model_totals[model] = model_totals.get(model, 0.0) + credit
        today = datetime.now().date()
        today_key = today.strftime("%Y-%m-%d")
        today_total = sum(r.get("today", 0.0) for r in ok_rows)
        week_total = sum(
            v
            for r in ok_rows
            for k, v in ((day["date"], day["credits"]) for day in r["daily"])
            if (today - datetime.strptime(k, "%Y-%m-%d").date()).days < 7
        )
        month_total = sum(
            v for r in ok_rows for k, v in ((day["date"], day["credits"]) for day in r["daily"]) if k.startswith(today_key[:7])
        )
        by_account = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "credits": round(sum(d["credits"] for d in r["daily"]), 2),
                "calls": r.get("totalCalls", 0),
            }
            for r in ok_rows
        ]
        by_account.sort(key=lambda r: -r["credits"])
        token_seen = any(r.get("tokenAvailable") for r in ok_rows)
        token_totals = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        today_tokens = {"input": 0, "output": 0, "cacheRead": 0}
        for r in ok_rows:
            for key in token_totals:
                token_totals[key] += int((r.get("tokenTotals") or {}).get(key) or 0)
            for key in today_tokens:
                today_tokens[key] += int((r.get("todayTokens") or {}).get(key) or 0)
        token_totals["total"] = token_totals["input"] + token_totals["output"]
        requests: list[dict] = []
        for r in ok_rows:
            for item in r.get("requests") or []:
                requests.append({**item, "account": r.get("name")})
        requests.sort(key=lambda r: -r["ts"])
        return {
            "ok": True,
            "days": days,
            "today": round(today_total, 2),
            "week": round(week_total, 2),
            "month": round(month_total, 2),
            "tokenAvailable": token_seen,
            "tokenTotals": token_totals if token_seen else None,
            "todayTokens": today_tokens if token_seen else None,
            "cacheHitRate": (
                (token_totals["cacheRead"] / token_totals["input"]) if token_seen and token_totals["input"] else None
            ),
            "daily": [
                {"date": k, "credits": round(v["credits"], 2), "models": v["models"]} for k, v in sorted(daily.items())
            ],
            "models": sorted(
                ({"model": m, "credits": round(c, 2)} for m, c in model_totals.items()),
                key=lambda r: -r["credits"],
            ),
            "byAccount": by_account,
            "accounts": per_account,
            "requests": requests[:100],
            "totalCalls": sum(r.get("totalCalls", 0) for r in ok_rows),
            "fetchedAt": util.now_ms(),
        }

    return _cached(("wb-usage-all", "*", int(days)), produce, force)
