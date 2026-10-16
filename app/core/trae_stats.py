#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trae 用量统计：按会话的 Token / 积分消耗与积分包到期。

- `POST /trae/api/v1/pay/query_user_usage_group_by_session`
  请求：`{"start_time": <unix秒>, "end_time": <unix秒>, "page_size": N, "page_num": n, "usage_type": [7]}`
  响应：`{"total": n, "user_usage_group_by_sessions": [{usage_time, credits_float,
  model_name, extra_info: {input_token, output_token, cache_read_token, cache_write_token},
  user_input_preview, ...}]}`
- `POST /trae/api/v2/pay/expired_ents`  积分包近期到期

鉴权沿用 `Cloud-IDE-JWT` 前缀头；统计结果进程内缓存 10 分钟。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from . import store, util

USAGE_URL = "https://api.trae.cn/trae/api/v1/pay/query_user_usage_group_by_session"
EXPIRED_URL = "https://api.trae.cn/trae/api/v2/pay/expired_ents"
# 权益包（积分包）明细：剩余积分 = credits_limit - usage.credits_amount
CREDITS_URL = "https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage"
# 包分类：该 product_id 为 Work（工作流）积分，其余为通用积分
WORK_PRODUCT_ID = 209
# 无到期时间的包按长期有效处理（参与排序用哨兵时间戳，展示时识别为长期有效）
FOREVER_TS = 4102444800

# 单页大小必须与官网控制台一致：该接口按 Web 路由做参数校验，
# 大分页会直接回参数错误而不是少返数据。
USAGE_PAGE_SIZE = 20
USAGE_MAX_PAGES = 50
# 拉取窗口分块：区间过大同样会被参数校验拒绝（按 30 天切分，块间无缝不重叠）
CHUNK_DAYS = 30

CACHE_TTL_SECONDS = 600
_CACHE: dict[tuple, tuple[float, object]] = {}

# 该接口走官网 Web 形态：浏览器 UA + origin/referer + sec-fetch，
# 且**不能**带 IDE 客户端指纹头（否则按 Web 路由校验不通过）。
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _headers(account: dict) -> dict:
    jwt = str(account.get("jwt") or "")
    if not jwt.startswith("Cloud-IDE-JWT "):
        jwt = f"Cloud-IDE-JWT {jwt}"
    return {
        "Authorization": jwt,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "User-Agent": WEB_USER_AGENT,
        "Origin": "https://www.trae.cn",
        "Referer": "https://www.trae.cn/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


def _code_of(resp: dict):
    """业务码：可能是数字或字符串数字；无 code 表示按裸结构返回。"""
    raw = resp.get("code") if isinstance(resp, dict) else None
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _cached(key: tuple, producer, force: bool = False):
    hit = _CACHE.get(key)
    now = time.time()
    if not force and hit and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    data = producer()
    if isinstance(data, dict) and data.get("ok"):
        _CACHE[key] = (now, data)
    return data


def peek_all_credits():
    """只读缓存里的全账号积分包快照；未命中返回 None。

    首屏聚合接口用它取数：统计要打外部网络，绝不能落在首屏同步路径上。
    """
    return _peek(("trae-credits-all", "*"))


def peek_all_usage(days: int = 31):
    """只读缓存里指定天数的全账号用量快照；未命中返回 None。"""
    return _peek(("trae-usage-all", "*", int(days)))


def _peek(key: tuple):
    """只读缓存：命中且未过期才返回，否则 None —— 绝不触发外部请求。"""
    hit = _CACHE.get(key)
    if not hit:
        return None
    if time.time() - hit[0] >= CACHE_TTL_SECONDS:
        return None
    return hit[1]


def stats_invalidate() -> None:
    _CACHE.clear()


def _num(value) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


def fetch_usage(account: dict, days: int = 30, force: bool = False) -> dict:
    """近 N 天按会话用量：逐日 Token / 积分 + 模型分布 + 会话 TopN。"""

    def produce() -> dict:
        now = datetime.now()
        end_ts = int(now.timestamp())
        start_ts = int((now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0).timestamp())
        headers = _headers(account)

        daily: dict[str, dict] = {}
        sessions: list[dict] = []
        seen: set[str] = set()
        chunk_end = end_ts
        while True:
            chunk_start = max(start_ts, chunk_end - CHUNK_DAYS * 86400 + 1)
            chunk_count = 0
            chunk_total: int | None = None
            for page in range(1, USAGE_MAX_PAGES + 1):
                body = {
                    "start_time": chunk_start,
                    "end_time": chunk_end,
                    "page_size": USAGE_PAGE_SIZE,
                    "page_num": page,
                    "usage_type": [7],
                }
                resp = util.http_json(USAGE_URL, "POST", body=body, headers=headers)
                code = _code_of(resp)
                if code is not None and code not in (0, 200):
                    detail = str(resp.get("message") or resp.get("msg") or "").strip()[:120]
                    hint = "（分页/区间参数被服务端拒绝）" if code in (400, 9004) else ""
                    return {
                        "ok": False,
                        "id": account.get("id"),
                        "name": account.get("name") or account.get("user_id") or "",
                        "error": f"用量查询失败（code={code}）{hint}" + (f"：{detail}" if detail else ""),
                    }
                items = resp.get("user_usage_group_by_sessions") or []
                if chunk_total is None and isinstance(resp.get("total"), int):
                    chunk_total = resp["total"]
                if not items:
                    break
                for item in items:
                    ts = int(_num(item.get("usage_time")))
                    if ts <= 0:
                        continue
                    rid = str(item.get("session_id") or item.get("id") or f"{ts}-{len(seen)}")
                    if rid in seen:
                        continue
                    seen.add(rid)
                    extra = item.get("extra_info") if isinstance(item.get("extra_info"), dict) else {}
                    credits = _num(item.get("credits_float") or item.get("amount_float"))
                    date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    slot = daily.setdefault(
                        date,
                        {"credits": 0.0, "sessions": 0, "input": 0, "output": 0, "cacheRead": 0, "models": {}},
                    )
                    slot["credits"] += credits
                    slot["sessions"] += 1
                    slot["input"] += int(_num(extra.get("input_token")))
                    slot["output"] += int(_num(extra.get("output_token")))
                    slot["cacheRead"] += int(_num(extra.get("cache_read_token")))
                    model = str(item.get("model_name") or "未知模型")
                    slot["models"][model] = slot["models"].get(model, 0) + int(
                        _num(extra.get("input_token")) + _num(extra.get("output_token"))
                    )
                    sessions.append(
                        {
                            "ts": ts * 1000,
                            "model": model,
                            "mode": str(item.get("mode") or ""),
                            "credits": round(credits, 2),
                            "input": int(_num(extra.get("input_token"))),
                            "output": int(_num(extra.get("output_token"))),
                            "cacheRead": int(_num(extra.get("cache_read_token"))),
                            "cacheWrite": int(_num(extra.get("cache_write_token"))),
                            "preview": str(item.get("user_input_preview") or item.get("title") or "")[:200],
                        }
                    )
                chunk_count += len(items)
                # total 缺失时按「不足一页」判结束，避免一路翻到空页
                if chunk_total is not None:
                    if chunk_count >= chunk_total:
                        break
                elif len(items) < USAGE_PAGE_SIZE:
                    break
            if chunk_start <= start_ts:
                break
            chunk_end = chunk_start - 1

        today_key = now.strftime("%Y-%m-%d")
        model_totals: dict[str, int] = {}
        for slot in daily.values():
            for model, tokens in slot["models"].items():
                model_totals[model] = model_totals.get(model, 0) + tokens
        sessions.sort(key=lambda r: -(r["input"] + r["output"] + r["cacheRead"]))

        today_credits = daily.get(today_key, {}).get("credits", 0.0)
        week_cut = now.date() - timedelta(days=6)
        week_credits = sum(
            v["credits"] for k, v in daily.items() if datetime.strptime(k, "%Y-%m-%d").date() >= week_cut
        )
        month_credits = sum(v["credits"] for k, v in daily.items() if k.startswith(now.strftime("%Y-%m")))
        token_totals = {
            "input": sum(v["input"] for v in daily.values()),
            "output": sum(v["output"] for v in daily.values()),
            "cacheRead": sum(v["cacheRead"] for v in daily.values()),
        }
        # 展示口径：输入已含缓存读，总量 = 输入 + 输出（与官方控制台一致，不重复计缓存）
        token_totals["total"] = token_totals["input"] + token_totals["output"]
        return {
            "ok": True,
            "id": account.get("id"),
            "name": account.get("name") or account.get("user_id") or "",
            "days": int(days),
            "today": round(today_credits, 2),
            "week": round(week_credits, 2),
            "month": round(month_credits, 2),
            "tokenTotals": token_totals,
            "todayTokens": {
                "input": daily.get(today_key, {}).get("input", 0),
                "output": daily.get(today_key, {}).get("output", 0),
                "cacheRead": daily.get(today_key, {}).get("cacheRead", 0),
            },
            "cacheHitRate": (token_totals["cacheRead"] / token_totals["input"]) if token_totals["input"] else None,
            "daily": [
                {
                    "date": k,
                    "credits": round(v["credits"], 2),
                    "sessions": v["sessions"],
                    "input": v["input"],
                    "output": v["output"],
                    "cacheRead": v["cacheRead"],
                    "tokens": v["input"] + v["output"],
                    "models": v["models"],
                }
                for k, v in sorted(daily.items())
            ],
            "models": sorted(
                ({"model": m, "tokens": t} for m, t in model_totals.items()),
                key=lambda r: -r["tokens"],
            ),
            "sessions": sessions[:10],
            "totalSessions": len(seen),
            "fetchedAt": util.now_ms(),
        }

    return _cached(("trae-usage", str(account.get("id") or ""), int(days)), produce, force)


def fetch_all_usage(days: int = 30, force: bool = False) -> dict:
    """全账号 Trae 用量聚合（积分 + Token 双维度，含跨账号合计）。"""

    def produce() -> dict:
        accounts = store.load_accounts(store.PLATFORM_TRAE)
        per_account = [fetch_usage(acc, days, force) for acc in accounts]
        ok_rows = [r for r in per_account if r.get("ok")]
        daily: dict[str, dict] = {}
        sessions: list[dict] = []
        model_tokens: dict[str, int] = {}
        for row in ok_rows:
            for day in row["daily"]:
                slot = daily.setdefault(
                    day["date"], {"credits": 0.0, "sessions": 0, "input": 0, "output": 0, "cacheRead": 0}
                )
                slot["credits"] += day["credits"]
                slot["sessions"] += day["sessions"]
                slot["input"] += day["input"]
                slot["output"] += day["output"]
                slot["cacheRead"] += day["cacheRead"]
            for item in row.get("models") or []:
                model_tokens[item["model"]] = model_tokens.get(item["model"], 0) + item["tokens"]
            for item in row.get("sessions") or []:
                sessions.append({**item, "account": row.get("name")})
        now = datetime.now()
        today_key = now.strftime("%Y-%m-%d")
        by_account = [
            {"id": r.get("id"), "name": r.get("name"), "credits": round(r.get("month") or 0.0, 2)} for r in ok_rows
        ]
        by_account.sort(key=lambda r: -r["credits"])
        totals = {
            "input": sum(v["input"] for v in daily.values()),
            "output": sum(v["output"] for v in daily.values()),
            "cacheRead": sum(v["cacheRead"] for v in daily.values()),
        }
        totals["total"] = totals["input"] + totals["output"]
        return {
            "ok": True,
            "days": days,
            "today": round(sum(r.get("today", 0.0) for r in ok_rows), 2),
            "week": round(sum(r.get("week", 0.0) for r in ok_rows), 2),
            "month": round(sum(r.get("month", 0.0) for r in ok_rows), 2),
            "tokenTotals": totals,
            "todayTokens": {
                "input": sum((r.get("todayTokens") or {}).get("input", 0) for r in ok_rows),
                "output": sum((r.get("todayTokens") or {}).get("output", 0) for r in ok_rows),
                "cacheRead": sum((r.get("todayTokens") or {}).get("cacheRead", 0) for r in ok_rows),
            },
            "cacheHitRate": (totals["cacheRead"] / totals["input"]) if totals["input"] else None,
            "daily": [
                {
                    "date": k,
                    "credits": round(v["credits"], 2),
                    "sessions": v["sessions"],
                    "input": v["input"],
                    "output": v["output"],
                    "cacheRead": v["cacheRead"],
                    "tokens": v["input"] + v["output"],
                }
                for k, v in sorted(daily.items())
            ],
            "models": sorted(
                ({"model": m, "tokens": t} for m, t in model_tokens.items()), key=lambda r: -r["tokens"]
            ),
            "sessions": sorted(sessions, key=lambda r: -(r["input"] + r["output"] + r["cacheRead"]))[:10],
            "totalSessions": sum(r.get("totalSessions", 0) for r in ok_rows),
            "byAccount": by_account,
            "accounts": per_account,
            "fetchedAt": util.now_ms(),
        }

    return _cached(("trae-usage-all", "*", int(days)), produce, force)


def fetch_expiring(account: dict) -> dict:
    """积分包近期到期列表（宽容解析，失败返回空列表）。"""

    def produce() -> dict:
        resp = util.http_json(EXPIRED_URL, "POST", body={}, headers=_headers(account))
        code = resp.get("code")
        if isinstance(code, int) and code not in (0, 200):
            return {"ok": True, "packages": [], "error": f"code={code}"}
        data = resp.get("data")
        items = data if isinstance(data, list) else (data or {}).get("ents") if isinstance(data, dict) else []
        packages = []
        for item in items if isinstance(items, list) else []:
            expire_ts = util.norm_ts(
                item.get("expire_time") or item.get("expireTime") or item.get("expired_at") or item.get("end_time")
            )
            packages.append(
                {
                    "name": str(item.get("name") or item.get("ent_name") or "积分包"),
                    "remaining": _num(item.get("remaining") or item.get("balance") or item.get("amount")),
                    "expireTs": expire_ts,
                    "expireText": datetime.fromtimestamp(expire_ts / 1000).strftime("%m/%d %H:%M") if expire_ts else "",
                }
            )
        packages.sort(key=lambda r: (r["expireTs"] or 9e15))
        return {"ok": True, "packages": packages[:10]}

    return _cached(("trae-expiring", str(account.get("id") or "")), produce)


# ---------------------------------------------------------------------------
# 积分包与剩余积分（与 WorkBuddy 侧同构的输出，前端复用同一套账号卡片渲染）
# ---------------------------------------------------------------------------


def _opt_num(value) -> float | None:
    """宽容取数：非数字返回 None（与「0」区分）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _dig(obj, keys: tuple, depth: int = 0):
    """按候选键名逐层下钻（响应包装层随版本变化，键名大小写不敏感）。"""
    if depth > 6 or not isinstance(obj, dict):
        return None
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None:
            return value
    for value in obj.values():
        found = _dig(value, keys, depth + 1)
        if found is not None:
            return found
    return None


def _pack_source(pack: dict) -> str:
    """积分包来源标签：付费购买 / 每日签到 / 每月登录 / 兑换，兜底用官方描述。"""
    base = pack.get("entitlement_base_info") if isinstance(pack.get("entitlement_base_info"), dict) else {}
    charge = _opt_num(base.get("charge_amount")) or 0.0
    group_name = str(pack.get("group_name") or "")
    desc = str(pack.get("display_desc") or "")
    if charge > 0:
        return "会员/购买"
    combined = group_name + desc
    if "签到" in combined:
        return "每日签到"
    if "登录" in combined:
        return "每月登录"
    if "兑换" in combined or "redeem" in combined.lower():
        return "兑换"
    return desc or group_name or "积分包"


def fetch_credits(account: dict, force: bool = False) -> dict:
    """单账号 Trae 积分包与剩余积分。输出结构与 WorkBuddy 侧一致：

    `{"ok", "id", "name", "balance", "packages": [{name, kind, total, remaining,
    expireTs, expireText}], "general", "work", "totalLimit", "fetchedAt"}`
    """

    def produce() -> dict:
        jwt = str(account.get("jwt") or "").strip()
        label = account.get("name") or account.get("user_id") or ""
        if not jwt:
            return {"ok": False, "id": account.get("id"), "name": label, "error": "账号缺少 JWT"}
        resp = util.http_json(
            CREDITS_URL, "POST", body={"require_usage": True, "req_source": 2}, headers=_headers(account)
        )
        code = _code_of(resp)
        if code is not None and code not in (0, 200):
            detail = str(resp.get("message") or resp.get("msg") or "").strip()[:120]
            return {
                "ok": False,
                "id": account.get("id"),
                "name": label,
                "error": f"积分查询失败（code={code}）" + (f"：{detail}" if detail else ""),
            }
        packs_raw = _dig(resp, ("user_entitlement_pack_list",))
        if not isinstance(packs_raw, list):
            return {"ok": False, "id": account.get("id"), "name": label, "error": "响应中缺少积分包列表"}

        now_ts = int(time.time())
        total = general = work = total_limit = 0.0
        packages: list[dict] = []
        membership_expire: int | None = None
        for pack in packs_raw:
            if not isinstance(pack, dict):
                continue
            base = pack.get("entitlement_base_info") if isinstance(pack.get("entitlement_base_info"), dict) else {}
            quota = base.get("quota") if isinstance(base.get("quota"), dict) else {}
            limit = _opt_num(quota.get("credits_limit"))
            if limit is None:
                continue
            usage = pack.get("usage") if isinstance(pack.get("usage"), dict) else {}
            used = _opt_num(usage.get("credits_amount")) or 0.0
            remaining = max(0.0, limit - used)
            product_id = int(_opt_num(base.get("product_id")) or 0)
            kind = "Work" if product_id == WORK_PRODUCT_ID else "通用"
            if "会员" in str(pack.get("group_name") or "") + str(pack.get("display_desc") or ""):
                end = _opt_num(base.get("end_time")) or _opt_num(pack.get("expire_time"))
                if end and (membership_expire is None or int(end) > membership_expire):
                    membership_expire = int(end)
            expire_raw = _opt_num(pack.get("expire_time")) or 0.0
            # 余额只统计「仍有剩余 且 未过期」的包：与卡片/到期列表同源，
            # 把已过期包算进来会让「剩余积分」虚高。
            if remaining <= 0 or (expire_raw > 0 and expire_raw <= now_ts):
                continue
            total += remaining
            total_limit += limit
            if kind == "Work":
                work += remaining
            else:
                general += remaining
            expire_ts = int(expire_raw * 1000) if expire_raw > 0 else None
            packages.append(
                {
                    "name": _pack_source(pack),
                    "code": str(base.get("product_id") or ""),
                    "kind": kind,
                    "total": round(limit, 2),
                    "remaining": round(remaining, 2),
                    "used": round(used, 2),
                    "expireTs": expire_ts,
                    "expireText": (
                        datetime.fromtimestamp(expire_ts / 1000).strftime("%m/%d %H:%M")
                        if expire_ts
                        else "长期有效"
                    ),
                }
            )
        packages.sort(key=lambda r: (r["expireTs"] or FOREVER_TS * 1000, -r["remaining"]))
        return {
            "ok": True,
            "id": account.get("id"),
            "name": label,
            "variant": "trae",
            "balance": round(total, 2),
            "general": round(general, 2),
            "work": round(work, 2),
            "totalLimit": round(total_limit, 2),
            "membershipExpireTs": membership_expire * 1000 if membership_expire else None,
            "packages": packages,
            "fetchedAt": util.now_ms(),
        }

    return _cached(("trae-credits", str(account.get("id") or "")), produce, force)


def fetch_all_credits(force: bool = False) -> dict:
    """全账号 Trae 积分聚合（失败账号不阻塞其余）。"""

    def produce() -> dict:
        accounts = store.load_accounts(store.PLATFORM_TRAE)
        rows = [fetch_credits(acc, force) for acc in accounts]
        ok_rows = [r for r in rows if r.get("ok")]
        return {
            "ok": True,
            "accounts": rows,
            "totalBalance": round(sum(r.get("balance") or 0.0 for r in ok_rows), 2),
            "packageCount": sum(len(r.get("packages") or []) for r in ok_rows),
            "fetchedAt": util.now_ms(),
        }

    return _cached(("trae-credits-all", "*"), produce, force)
