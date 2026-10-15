#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心纯函数单元测试。

覆盖两个平台的协议解析与策略判定：WorkBuddy 的档位 / 时钟 / 窗口抽签 / 状态判定，
Trae 的 JWT 解析 / 设备指纹 / 错误分类与冷却 / 积分三层兜底，
以及 OAuth 的 PKCE 与回调域名白名单。

用法：
    python3 tests/test_unit.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(os.path.dirname(HERE), "app")
sys.path.insert(0, APP_DIR)

os.environ["QD_DATA_DIR"] = tempfile.mkdtemp(prefix="qd-unit-")

from core import (  # noqa: E402
    oauth,
    store,
    trae_checkin,
    trae_identity,
    trae_stats,
    trae_storage,
    util,
    variant,
    wb_checkin,
    wb_stats,
)

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def eq(name: str, actual, expected) -> None:
    check(name, actual == expected, f"实际 {actual!r} != 期望 {expected!r}")


# ===========================================================================
# 一、档位（variant）
# ===========================================================================


def test_variant() -> None:
    eq("parse 缺省回落国内版", variant.parse(None), "cn")
    eq("parse 空串回落国内版", variant.parse(""), "cn")
    eq("parse 未知值回落国内版", variant.parse("unknown"), "cn")
    eq("parse ai", variant.parse("AI"), "ai")
    eq("parse intl", variant.parse("intl"), "ai")

    eq("from_account 空账号", variant.from_account({}), "cn")
    eq("from_account 显式 ai", variant.from_account({"variant": "ai"}), "ai")
    eq(
        "from_account 显式字段优先于域名",
        variant.from_account({"variant": "cn", "domain": "www.workbuddy.ai"}),
        "cn",
    )
    eq("from_account 域名兜底", variant.from_account({"domain": "www.workbuddy.ai"}), "ai")
    eq(
        "from_account 只认后缀不被相似域名误判",
        variant.from_account({"domain": "www.workbuddy.ai.evil.com"}),
        "cn",
    )

    eq("国内版基址", variant.api_endpoint("cn"), "https://www.codebuddy.cn")
    eq("国际版基址", variant.api_endpoint("ai"), "https://www.workbuddy.ai")
    eq("接口前缀两档位相同", variant.api_prefix("cn"), "/v2/plugin")

    eq(
        "国内版 billing 路径只有一个候选",
        variant.billing_paths("cn", "/v2/billing/meter/daily-checkin"),
        ["/v2/billing/meter/daily-checkin"],
    )
    eq(
        "国际版 billing 路径先去 /v2 再回落",
        variant.billing_paths("ai", "/v2/billing/meter/daily-checkin"),
        ["/billing/meter/daily-checkin", "/v2/billing/meter/daily-checkin"],
    )

    check("国内版支持签到", variant.supports_checkin("cn"))
    check("国际版不支持签到", not variant.supports_checkin("ai"))


# ===========================================================================
# 二、工具（util）
# ===========================================================================


def test_util() -> None:
    eq("parse_clock 标准值", util.parse_clock("09:05"), (9, 5))
    eq("parse_clock 单位数", util.parse_clock("9:5"), (9, 5))
    eq("parse_clock 空串", util.parse_clock(""), None)
    eq("parse_clock 越界小时", util.parse_clock("24:00"), None)
    eq("parse_clock 越界分钟", util.parse_clock("23:60"), None)
    eq("parse_clock 非数字", util.parse_clock("ab:cd"), None)
    eq("parse_clock 多余字符", util.parse_clock("09:05:00"), None)

    eq("norm_ts 秒转毫秒", util.norm_ts(1_700_000_000), 1_700_000_000_000)
    eq("norm_ts 毫秒原样", util.norm_ts(1_700_000_000_000), 1_700_000_000_000)
    eq("norm_ts 字符串", util.norm_ts("1700000000"), 1_700_000_000_000)
    eq("norm_ts 非法", util.norm_ts("abc"), None)
    eq("norm_ts None", util.norm_ts(None), None)

    eq("is_route_missing 顶层 404", util.is_route_missing({"code": 404}), True)
    eq("is_route_missing 字符串 404", util.is_route_missing({"code": "404"}), True)
    eq("is_route_missing data 层 404", util.is_route_missing({"data": {"code": 404}}), True)
    eq("is_route_missing 401 不算路径缺失", util.is_route_missing({"code": 401}), False)
    eq("is_route_missing 传输错误不算", util.is_route_missing({"code": -1}), False)

    eq("local_date_str 长度", len(util.local_date_str()), 10)


# ===========================================================================
# 三、WorkBuddy 签到判定（wb_checkin）
# ===========================================================================


def test_wb_checkin() -> None:
    # 未授权判定
    check("401 判定为未授权", wb_checkin.is_unauthorized({"code": 401}))
    check("403 判定为未授权", wb_checkin.is_unauthorized({"code": 403}))
    check("message 含 token 判定为未授权", wb_checkin.is_unauthorized({"message": "token 已失效"}))
    check("正常响应不算未授权", not wb_checkin.is_unauthorized({"code": 0, "message": "ok"}))

    # 国际版 inactive 文案
    check("未开启判定 inactive", wb_checkin.is_inactive_message("签到活动未开启"))
    check("inactive 英文", wb_checkin.is_inactive_message("Inactive"))
    check("普通错误不算 inactive", not wb_checkin.is_inactive_message("参数错误"))

    # 状态响应解析
    status = wb_checkin.status_from_response({"code": 0, "data": {"today_checked_in": True}})
    check("状态解析 todayCheckedIn", status is not None and status["todayCheckedIn"] is True)
    eq("非 0 code 返回 None", wb_checkin.status_from_response({"code": 500}), None)
    eq("200 也算成功", wb_checkin.status_from_response({"code": 200}) is not None, True)

    # 窗口
    eq("窗口生效", wb_checkin.window_minutes({"checkin_start": "09:00", "checkin_end": "12:00"}), (540, 720))
    eq("窗口缺失字段不生效", wb_checkin.window_minutes({"checkin_start": "", "checkin_end": ""}), None)
    eq("start >= end 不生效", wb_checkin.window_minutes({"checkin_start": "12:00", "checkin_end": "09:00"}), None)

    # 确定性抽签：同一天同一账号结果稳定，不同账号错峰
    day = "2026-10-15"
    a1 = wb_checkin.target_minute(day, "account-a", 540, 720)
    a2 = wb_checkin.target_minute(day, "account-a", 540, 720)
    b1 = wb_checkin.target_minute(day, "account-b", 540, 720)
    eq("抽签确定性", a1, a2)
    check("抽签落在窗口内", 540 <= a1 < 720)
    check("抽签不同账号不同值", a1 != b1)

    # 账号排除
    cfg = {"workbuddy": {"excluded_account_ids": ["x1"]}}
    check("被排除账号不参与", not wb_checkin.allows_auto_checkin({"id": "x1"}, cfg))
    check("未排除账号参与", wb_checkin.allows_auto_checkin({"id": "x2"}, cfg))
    check("国际版不参与", not wb_checkin.allows_auto_checkin({"id": "x3", "variant": "ai"}, cfg))


# ===========================================================================
# 四、Trae JWT（trae_identity）
# ===========================================================================


def _make_jwt(payload: dict) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{seg}.signature"


def test_trae_jwt() -> None:
    jwt = _make_jwt({"data": {"id": "88888"}, "exp": int(time.time()) + 7200})
    info = trae_identity.jwt_parse(jwt)
    eq("JWT 解析 user_id", info["user_id"], "88888")
    check("JWT 剩余小时数合理", info["exp_hours"] is not None and 1.9 < info["exp_hours"] < 2.1)

    # 前缀兼容
    prefixed = trae_identity.jwt_parse(f"Cloud-IDE-JWT {jwt}")
    eq("Cloud-IDE-JWT 前缀", prefixed["user_id"], "88888")
    bearer = trae_identity.jwt_parse(f"Bearer {jwt}")
    eq("Bearer 前缀", bearer["user_id"], "88888")

    # 过期
    expired = trae_identity.jwt_parse(_make_jwt({"data": {"id": "1"}, "exp": int(time.time()) - 100}))
    check("过期 JWT 剩余为负", (expired["exp_hours"] or 0) < 0)

    # 非法
    bad = trae_identity.jwt_parse("not-a-jwt")
    eq("非法 JWT 返回空", bad["user_id"], None)

    # auth_id 兜底
    fallback = trae_identity.jwt_parse(_make_jwt({"auth_id": "abc123"}))
    eq("auth_id 兜底", fallback["user_id"], "abc123")

    # 身份线索：JWT 里带昵称 / 邮箱时带出（多账号区分用）
    profiled = trae_identity.jwt_parse(
        _make_jwt({"data": {"id": "88888", "name": "齊大", "email": "a@b.c"}, "exp": int(time.time()) + 7200})
    )
    eq("JWT 昵称提取", profiled["name"], "齊大")
    eq("JWT 邮箱提取", profiled["email"], "a@b.c")
    bare = trae_identity.jwt_parse(_make_jwt({"data": {"id": "88888"}}))
    eq("无身份字段时为 None", (bare["name"], bare["email"]), (None, None))

    # 状态分级
    eq("jwt_status ok", trae_identity.jwt_status(48.0), "ok")
    eq("jwt_status warn", trae_identity.jwt_status(12.0), "warn")
    eq("jwt_status expired", trae_identity.jwt_status(-1.0), "expired")
    eq("jwt_status unknown", trae_identity.jwt_status(None), "unknown")


def test_trae_device() -> None:
    first = trae_identity.derive_device("user-1")
    second = trae_identity.derive_device("user-1")
    other = trae_identity.derive_device("user-2")
    eq("设备派生确定性", first, second)
    check("不同账号设备不同", first["device_id"] != other["device_id"])
    eq("device_id 长度 15", len(first["device_id"]), 15)
    check("device_id 全数字", first["device_id"].isdigit())
    eq("session_id 长度 64", len(first["session_id"]), 64)
    # UUID v4 版本位与变体位
    uuid = first["market_user_id"]
    eq("market_user_id 形态", len(uuid), 36)
    check("UUID 版本位为 4", uuid[14] == "4")
    check("UUID 变体位正确", uuid[19] in ("8", "9", "a", "b"))


# ===========================================================================
# 五、Trae 签到判定（trae_checkin）
# ===========================================================================


def test_trae_classify() -> None:
    eq("PlanLimit", trae_checkin.classify_error(200, 1005), ("PlanLimit", 43_200))
    eq("SoftRate", trae_checkin.classify_error(429, None), ("SoftRate", 60))
    eq("SessionDead", trae_checkin.classify_error(401, None), ("SessionDead", -1))
    eq("NotFound", trae_checkin.classify_error(404, None), ("NotFound", 60))
    eq("Server", trae_checkin.classify_error(502, None), ("Server", 600))
    eq("Client", trae_checkin.classify_error(403, None), ("Client", 600))
    eq("BusinessError", trae_checkin.classify_error(200, 7), ("BusinessError", 300))
    eq("Unknown（传输层异常）", trae_checkin.classify_error(0, None), ("Unknown", 0))


def test_trae_reward() -> None:
    eq("顶层奖励字段", trae_checkin.parse_claim_reward({"reward": 10}), 10)
    eq("data 层优先于顶层", trae_checkin.parse_claim_reward({"data": {"delta": 5}, "credits": 99}), 5)
    eq("数字串", trae_checkin.parse_claim_reward({"delta": "8"}), 8)
    eq("整值 float", trae_checkin.parse_claim_reward({"delta": 3.0}), 3)
    eq("小数拒绝", trae_checkin.parse_claim_reward({"delta": 3.5}), None)
    eq("负值拒绝", trae_checkin.parse_claim_reward({"delta": -1}), None)
    eq("bool 拒绝", trae_checkin.parse_claim_reward({"reward": True}), None)
    eq("无候选字段", trae_checkin.parse_claim_reward({"other": 1}), None)
    eq("None 输入", trae_checkin.parse_claim_reward(None), None)
    eq(
        "零值占位不挡住真实奖励",
        trae_checkin.parse_claim_reward({"code": 0, "data": {"credits": 0, "reward": 12}}),
        12,
    )
    eq(
        "多层信封下钻",
        trae_checkin.parse_claim_reward({"code": 0, "data": {"data": {"reward": 9}}}),
        9,
    )


def test_trae_credits_fallback() -> None:
    # 层1：claim 奖励字段命中 → credits 沿用签到前余额
    eq(
        "层1 claim_reward",
        trae_checkin.resolve_claim_credits({"reward": 5}, 100, None),
        (100, 5, "claim_reward"),
    )
    # 层2：复查余额差值
    eq(
        "层2 balance_diff",
        trae_checkin.resolve_claim_credits({}, 100, lambda: (True, 105)),
        (105, 5, "balance_diff"),
    )
    # 层2 负差 → 落层3
    eq(
        "层2 负差落层3",
        trae_checkin.resolve_claim_credits({}, 100, lambda: (True, 90)),
        (100, 100, "legacy"),
    )
    # 层3：签到前余额缺失
    eq(
        "层3 无余额",
        trae_checkin.resolve_claim_credits({}, None, None),
        (None, 0, "legacy"),
    )


def test_trae_envelope() -> None:
    payload = {"code": 0, "data": {"result": {"credits": 99}}, "credits": 1}
    eq("外层优先", trae_checkin.find_payload_field(payload, "credits"), 1)
    eq("字段被包裹时下钻", trae_checkin.find_payload_field({"data": {"result": {"credits": 42}}}, "credits"), 42)
    eq("不存在字段", trae_checkin.find_payload_field({"other": 1}, "credits"), None)

    eq("as_int 整数", trae_checkin.as_int_tolerant(5), 5)
    eq("as_int 整值 float", trae_checkin.as_int_tolerant(2.0), 2)
    eq("as_int 数字串", trae_checkin.as_int_tolerant("15"), 15)
    eq("as_int bool", trae_checkin.as_int_tolerant(True), None)
    eq("as_int 小数", trae_checkin.as_int_tolerant(1.5), None)
    eq("as_int 非数字串", trae_checkin.as_int_tolerant("abc"), None)
    eq("as_int None", trae_checkin.as_int_tolerant(None), None)


def test_trae_humanize() -> None:
    server_msg = "该设备绑定的账户数量已达上限。"
    out = trae_checkin.humanize_device_limit(server_msg)
    check("命中特征词保留原文", out.startswith(server_msg))
    check("命中特征词追加引导", "重新执行一次登录" in out)
    check("英文形态命中", "设备绑定数已达服务端上限" in trae_checkin.humanize_device_limit("device limit"))
    eq(
        "非设备绑定类「已达上限」不误伤",
        trae_checkin.humanize_device_limit("当前套餐使用额度已达上限"),
        "当前套餐使用额度已达上限",
    )
    eq("未命中原样透传", trae_checkin.humanize_device_limit("HTTP 502"), "HTTP 502")


def test_trae_cooldown() -> None:
    # 永久冷却（SessionDead）
    trae_checkin.save_cooldown("u-1", "SessionDead", -1, "jwt 被吊销")
    eq("SessionDead 永久冷却", trae_checkin.cooldown_active("u-1"), "SessionDead")

    # Server/Client 前两次只计数不冷却
    trae_checkin.save_cooldown("u-2", "Server", 600, "服务端错误")
    eq("Server 第 1 次不冷却", trae_checkin.cooldown_active("u-2"), None)
    trae_checkin.save_cooldown("u-2", "Server", 600, "服务端错误")
    eq("Server 第 2 次不冷却", trae_checkin.cooldown_active("u-2"), None)
    trae_checkin.save_cooldown("u-2", "Server", 600, "服务端错误")
    eq("Server 第 3 次进入冷却", trae_checkin.cooldown_active("u-2"), "Server")

    # 清除
    trae_checkin.clear_cooldown("u-2")
    eq("清除冷却", trae_checkin.cooldown_active("u-2"), None)

    # SoftRate 立即冷却
    trae_checkin.save_cooldown("u-3", "SoftRate", 60, "429")
    eq("SoftRate 立即冷却", trae_checkin.cooldown_active("u-3"), "SoftRate")
    trae_checkin.clear_cooldown("u-3")
    trae_checkin.clear_cooldown("u-1")


# ===========================================================================
# 六、存储
# ===========================================================================


def test_wb_receipt() -> None:
    """「服务端已签但本地无日志」→ 自动补 already 回执（今日状态联动的根修）。"""
    acc = {"id": "wb-receipt-1", "email": "receipt@example.com"}
    check("回执前今天无记录", not wb_checkin._today_answered(acc))
    wb_checkin._write_today_receipt(acc, "cn")
    check("回执后今天有记录", wb_checkin._today_answered(acc))
    eq(
        "回执结果为 already",
        store.latest_today_result(store.PLATFORM_WORKBUDDY, "wb-receipt-1"),
        "already",
    )
    before = len(store.load_checkin_logs())
    wb_checkin._write_today_receipt(acc, "cn")
    eq("重复回执不刷屏", len(store.load_checkin_logs()), before)
    check("其他账号不受影响", not wb_checkin._today_answered({"id": "wb-receipt-2"}))


def test_trae_cycle_rows() -> None:
    """一键签到（Trae 全量）的返回必须带逐账号明细，前端才能与 WB 同样渲染。"""
    import inspect

    src = inspect.getsource(trae_checkin.run_checkin_cycle)
    check("cycle 返回包含 rows 字段", '"rows": detail_rows' in src)
    check("明细含账号名", '"name": name_by_uid.get(uid, uid)' in src)


def test_store() -> None:
    saved = store.upsert_account(store.PLATFORM_TRAE, {"name": "A", "jwt": "jwt-a", "user_id": "u-a"})
    check("账号自动补 id", bool(saved.get("id")))
    same = store.upsert_account(store.PLATFORM_TRAE, {"id": saved["id"], "name": "A2", "jwt": "jwt-a"})
    eq("同 id 更新而非新增", same["name"], "A2")
    eq("账号数量仍为 1", len(store.load_accounts(store.PLATFORM_TRAE)), 1)
    check("删除账号", store.delete_account(store.PLATFORM_TRAE, saved["id"]))
    eq("删除后为空", len(store.load_accounts(store.PLATFORM_TRAE)), 0)

    store.add_checkin_log({"platform": "trae", "accountId": "u-x", "result": "success"})
    eq("今日结果可查", store.latest_today_result("trae", "u-x"), "success")
    eq("其他账号无结果", store.latest_today_result("trae", "u-none"), None)

    cfg = store.save_config({"recovery_interval_minutes": 15, "trae": {"checkin_gap_secs": 9}})
    eq("配置保存生效", cfg["recovery_interval_minutes"], 15)
    eq("嵌套配置保存生效", cfg["trae"]["checkin_gap_secs"], 9)
    eq("未指定字段回落默认", cfg["trae"]["retry"], 1)

    cfg2 = store.save_config({"checkin_start": "9:5", "checkin_end": "bad"})
    eq("时间段零填充", cfg2["checkin_start"], "09:05")
    eq("非法时间段归一为空", cfg2["checkin_end"], "")


# ===========================================================================
# 七、OAuth
# ===========================================================================


def test_oauth_pkce() -> None:
    verifier, challenge = oauth._pkce_pair()
    import hashlib as _h
    import base64 as _b

    eq("PKCE verifier 长度", len(verifier), 64)
    expected = _b.urlsafe_b64encode(_h.sha256(verifier.encode()).digest()).decode().rstrip("=")
    eq("PKCE challenge 与 verifier 匹配", challenge, expected)
    verifier2, _ = oauth._pkce_pair()
    check("PKCE verifier 随机", verifier != verifier2)


def test_oauth_host_allowlist() -> None:
    eq("白名单主域", oauth._validate_exchange_host("https://api.trae.com.cn"), "https://api.trae.com.cn")
    eq("白名单裸域", oauth._validate_exchange_host("api.trae.cn"), "https://api.trae.cn")
    eq("白名单带路径", oauth._validate_exchange_host("https://api.trae.com.cn/"), "https://api.trae.com.cn")
    for bad in ("evil.com", "https://evil.com", "api.trae.com.cn.evil.com"):
        try:
            oauth._validate_exchange_host(bad)
            check(f"伪造 host 被拒（{bad}）", False, "竟然通过了")
        except ValueError:
            check(f"伪造 host 被拒（{bad}）", True)


def test_oauth_find_token() -> None:
    eq(
        "顶层 AccessToken",
        oauth._find_token_value({"AccessToken": "a"}, ("AccessToken",)),
        "a",
    )
    eq(
        "Result 容器深挖",
        oauth._find_token_value({"Result": {"Token": "b"}}, ("Token",)),
        "b",
    )
    eq(
        "data.Data 多层深挖",
        oauth._find_token_value({"data": {"Data": {"Jwt": "c"}}}, ("Jwt",)),
        "c",
    )
    eq("列表内深挖", oauth._find_token_value({"list": [{"token": "d"}]}, ("token",)), "d")
    eq("无命中", oauth._find_token_value({"x": 1}, ("Token",)), None)
    eq("空串不算命中", oauth._find_token_value({"Token": "  "}, ("Token",)), None)


def test_oauth_volcano_error() -> None:
    body = {"Error": {"Code": "20403", "Message": "Device not match"}}
    eq("火山信封错误", oauth._volcano_error(body), ("20403", "Device not match", ""))
    eq("code=0 不算错误", oauth._volcano_error({"Error": {"Code": "0"}}), None)
    eq("无 Error 不算错误", oauth._volcano_error({"code": 0}), None)


def test_oauth_wb_domain() -> None:
    eq("国内版接受 cn 域", oauth._wb_domain_mismatch_error("cn", "www.codebuddy.cn"), None)
    eq("国内版拒绝 ai 域", oauth._wb_domain_mismatch_error("cn", "www.workbuddy.ai") is not None, True)
    eq("国际版接受 ai 域", oauth._wb_domain_mismatch_error("ai", "www.workbuddy.ai"), None)
    eq("国际版拒绝 cn 域", oauth._wb_domain_mismatch_error("ai", "www.codebuddy.cn") is not None, True)
    eq("空域放行（由调用方兼容）", oauth._wb_domain_mismatch_error("cn", ""), None)


def test_oauth_trae_start() -> None:
    result = oauth.trae_oauth_start()
    url = result["url"]
    check("登录 URL 指向授权页", url.startswith("https://www.trae.cn/authorization?"))
    for needle in (
        "client_id=ono9krqynydwx5",
        "code_challenge_method=S256",
        "login_channel=native_ide",
        "auth_callback_url=",
    ):
        check(f"登录 URL 含 {needle[:24]}", needle in url)
    check("登录 URL 携带 PKCE challenge", "code_challenge=" in url)
    with_store = oauth._TRAE_STATES.get(result["state"])
    check("会话已登记（含 verifier）", bool(with_store and with_store.get("pkce_verifier")))


def test_oauth_trae_callback_direct() -> None:
    """回调直接带 accessToken：不走网络，直接入库。"""
    store.save_accounts({"workbuddy": [], "trae": []})
    jwt = _make_jwt({"data": {"id": "777001"}, "exp": int(time.time()) + 3600})
    callback = f"http://127.0.0.1:17388/authorize?accessToken={jwt}&userId=777001&userName=测试用户"
    result = oauth.trae_oauth_callback(callback)
    eq("回调登录 user_id", result["userId"], "777001")
    eq("回调登录名称", result["name"], "测试用户")
    check("账号已入库", len(store.load_accounts("trae")) == 1)

    # uid 不一致必须拒绝（URL 参数可伪造，JWT 为权威）
    other_jwt = _make_jwt({"data": {"id": "777002"}, "exp": int(time.time()) + 3600})
    try:
        oauth.trae_oauth_callback(
            f"http://127.0.0.1:17388/authorize?accessToken={other_jwt}&userId=777001"
        )
        check("uid 不一致被拒绝", False, "竟然通过了")
    except ValueError:
        check("uid 不一致被拒绝", True)

    # 缺任何 token 参数的回调必须报错
    try:
        oauth.trae_oauth_callback("http://127.0.0.1:17388/authorize?userId=1")
        check("无 token 参数的回调被拒绝", False)
    except ValueError:
        check("无 token 参数的回调被拒绝", True)

    # 同 uid 再登录 → 更新既有账号而非新增
    again = oauth.trae_oauth_callback(callback)
    check("二次登录判定为合并", again.get("merged") is True)
    eq("合并不新增账号", len(store.load_accounts("trae")), 1)


def test_oauth_parse_callback_params() -> None:
    params = oauth._parse_callback_params(
        "http://127.0.0.1:17388/authorize?a=1&b=hello%20world&c=x%2By"
    )
    eq("参数解析", params.get("a"), "1")
    eq("percent 解码", params.get("b"), "hello world")
    eq("+ 不再视作空格", params.get("c"), "x+y")
    try:
        oauth._parse_callback_params("http://127.0.0.1/authorize")
        check("无查询参数报错", False)
    except ValueError:
        check("无查询参数报错", True)


def test_oauth_trae_precheck() -> None:
    """粘贴内容即时校验：粘错 / 截断 / 过期都要在提交前就说清楚。"""
    from urllib.parse import quote

    res = oauth.trae_callback_precheck("")
    check("空输入不通过", res["ok"] is False and "还没有粘贴" in res["reason"])

    res = oauth.trae_callback_precheck("127.0.0.1:17388/authorize")
    check("非完整 URL 不通过", res["ok"] is False)

    res = oauth.trae_callback_precheck("http://127.0.0.1:17388/authorize?isRedirect=true")
    check("无凭证参数不通过", res["ok"] is False and res["kind"] == "unknown")

    jwt = _make_jwt({"data": {"id": "778001"}, "exp": int(time.time()) + 3600})
    res = oauth.trae_callback_precheck(f"http://127.0.0.1:17388/authorize?accessToken={jwt}")
    check("直带 JWT 识别为 jwt", res["ok"] is True and res["kind"] == "jwt")
    check("JWT 形态不套用 AuthCode 时效", res["remainingSeconds"] is None)

    res = oauth.trae_callback_precheck("http://127.0.0.1/authorize?authCodeInfo=not-json")
    check("authCodeInfo 非 JSON 不通过", res["ok"] is False)

    empty_code = quote(json.dumps({"UserID": "1"}))
    res = oauth.trae_callback_precheck(f"http://127.0.0.1/authorize?authCodeInfo={empty_code}")
    check("缺少 AuthCode 字段不通过", res["ok"] is False)

    fresh = {"AuthCode": "code-fresh", "ExpireDuration": 600000, "CreateAt": int(time.time() * 1000)}
    res = oauth.trae_callback_precheck(
        f"http://127.0.0.1/authorize?authCodeInfo={quote(json.dumps(fresh))}"
    )
    check("新鲜 AuthCode 通过", res["ok"] is True and res["kind"] == "authcode")
    check("返回剩余秒数", isinstance(res["remainingSeconds"], int) and res["remainingSeconds"] > 500)

    stale = {"AuthCode": "code-old", "ExpireDuration": 600000, "CreateAt": int((time.time() - 900) * 1000)}
    res = oauth.trae_callback_precheck(
        f"http://127.0.0.1/authorize?authCodeInfo={quote(json.dumps(stale))}"
    )
    check("过期 AuthCode 判定 expired", res["ok"] is False and res["expired"] is True)
    check("过期提示指向重新授权", "重新" in res["reason"])

    # 过期码提交必须在发出交换前就被拦下（不能打出一串协议错误）
    try:
        oauth.trae_oauth_callback(f"http://127.0.0.1/authorize?authCodeInfo={quote(json.dumps(stale))}")
        check("过期 AuthCode 提交被拦截", False, "竟然放行了")
    except oauth.OAuthExpiredError:
        check("过期 AuthCode 提交被拦截", True)
    except Exception as exc:  # noqa: BLE001
        check("过期 AuthCode 提交被拦截", False, f"抛了别的异常: {type(exc).__name__}: {exc}")

    # 会话发起时间也可作为时效依据（回调不自带时间字段时）
    started = oauth.trae_oauth_start()
    with oauth._LOCK:
        oauth._TRAE_STATES[started["state"]]["created_at"] = int(time.time()) - 1800
    no_ts = {"AuthCode": "code-notime"}
    res = oauth.trae_callback_precheck(
        f"http://127.0.0.1/authorize?authCodeInfo={quote(json.dumps(no_ts))}"
    )
    check("回落会话时间判定过期", res["expired"] is True)


def test_oauth_trae_exchange_errors() -> None:
    """AuthCode 交换被服务端拒绝的多种形态：都要给出可读提示，
    设备签名类必须明确引导「改用手动粘贴 JWT」而不是抛通用字段错误。"""
    real_post = oauth._exchange_post

    def with_response(body):
        def fake(url, payload, device_id, empty_token_header=True):
            return body
        return fake

    def expect(kind: str, body: dict, needles: tuple) -> None:
        oauth._exchange_post = with_response(body)
        try:
            oauth.trae_exchange_authcode("code-x", "verifier-x", "https://api.trae.com.cn")
            check(kind, False, "竟然交换成功了")
            return
        except ValueError as exc:
            msg = str(exc)
        finally:
            oauth._exchange_post = real_post
        missed = [n for n in needles if n not in msg]
        check(kind, not missed, f"缺少 {missed}：{msg}")

    # 平铺字符串 code（真实场景常见：code 是 "20403" 而非整数）
    expect(
        "字符串 code=20403 引导手动 JWT",
        {"code": "20403", "message": "device proof required"},
        ("手动粘贴 JWT", "20403"),
    )
    # 火山信封 + 设备类
    expect(
        "火山信封 20405 引导手动 JWT",
        {"Error": {"Code": "20405", "Message": "device verify failed"}},
        ("手动粘贴 JWT", "20405"),
    )
    # 非设备的业务错误：照原样报 code，不误导用户去粘 JWT
    expect(
        "非设备错误按 code 报出",
        {"code": "40001", "message": "参数不合法"},
        ("40001", "参数不合法"),
    )
    # 200 但完全没有 token：必须带出响应片段，而不是干巴巴一句「未找到字段」
    expect(
        "无 token 时带出响应片段",
        {"code": "0", "message": "success", "data": {}},
        ("未返回有效凭证",),
    )
    # 响应里出现 device 关键字但无标准错误字段：仍判定为设备凭证缺失
    expect(
        "device 关键字兜底识别",
        {"result": {"hint": "DevicePublicKey missing"}},
        ("手动粘贴 JWT",),
    )


# 客户端凭据样本：由权威 AES 实现（Node crypto）按客户端格式加密生成，
# 用于校验本项目的纯标准库解密逐字节一致（通用头部 + 私有头部各一份）。
STORAGE_VECTOR_CN = (
    "dGMFEAAA/yCCqniuqAony0+5HwNQFTcCwW3OeQp38LsL+/aJmXeZ5CZbHJSStQeU64PHWzHPnetQ"
    "LOUpTPS3lFc6dRNdvFB8aahatkNeRADMfX6dqMQxvXrQQvrTsISQDylKe7TiWlxizGYa3P5K66qz"
    "MoZWQTYZUxT8no6stC7xoKK8dcEGNAy2HzmMiiqH+KeCJKXorMWvBjN+PHL3eIvCrFjyuuJ02JQi"
    "I+3ZzUbwdEa30+Bu4iwliP1azIB70VTrJ/Bb8O2A2n+IP1I7bqZZ7akfeFVBJhETa+T1lIVB5RQ3"
    "v6z/cN6AdFokjxyCYytWFA7HIazdFnIX5rSW5364i1/n5/ZqYz/CM8IOMa3Zq2b1pSHPrrNWAJ3V"
    "R3XHJjTxwni3aRPZgjZ5AvT1sXQMfWl/m/pg838KPCN0iRwY9BLYNCw="
)
STORAGE_VECTOR_PRIVATE = (
    "EjkgIAIDcV3ISTw2V5pbEWmVEA9jXjVy/fhwPnCO8aCNlDs2d04aWzRqER37MONThpUZb8tEcmq1"
    "2wsgjXI5ZiqTy1OCMyFBJ1ApqfMxVhYx9rLdS6xJK5zJU9PLhaggbVMejuaYo+uFq9JEcycMZJV9"
    "WF/HvXShbPIRoslbY0BM7PcUd1uaHfCCI3RjoNkf67KXgFIa9B6Fs5HaRFQvDiakWzNnfbhm+Gzp"
    "wIRzBj9joOEnDPM6p8pJncrQ9pWTrNmddO1/Qz5JFVQZ4BOpzY7VeJHuwVYNMv0wMWXyGN364R+P"
    "4AHJoXf+Yy9gw5mI5eUXQMChmx6sdQZDKLueDxH1s5Sy5zs+SzXlUqaoYWkzx4jTYVvFKhC38WOw"
    "I/brK5rkQbYskZ1Z4rxk93J0kbXNf3eqFRw5yOigFljHGzs7k5YTNpjW7TBExbFW0haIFuMVolY2"
)
STORAGE_PLAINTEXT_CN = (
    '{"token":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJkYXRhIjp7ImlkIjoiOTkwMDEif'
    'SwiZXhwIjoxNzk0NjM1OTI0fQ.signature-'
    'cn","userId":"9002","refreshToken":"demo-refresh-token-'
    'cn","expiredAt":1893456000000,"host":"api.trae.com.cn"}'
)


def test_trae_storage_decrypt() -> None:
    """客户端凭据解密：与权威 AES 实现逐字节一致（两种加密头部）。"""
    eq("通用头部解密一致", trae_storage.decrypt_tc_value(STORAGE_VECTOR_CN), STORAGE_PLAINTEXT_CN)
    private = trae_storage.decrypt_tc_value(STORAGE_VECTOR_PRIVATE)
    private_auth = json.loads(private)
    check(
        "私有头部解密一致",
        trae_identity.jwt_parse("Cloud-IDE-JWT " + private_auth["token"])["user_id"] == "99002",
        private[:80],
    )

    # 头部无法识别 / 数据损坏时必须报错，不能返回半截明文
    try:
        trae_storage.decrypt_tc_value("AAAA" + STORAGE_VECTOR_CN[8:])
        check("未知头部被拒绝", False, "竟然解出来了")
    except ValueError:
        check("未知头部被拒绝", True)

    corrupted = list(STORAGE_VECTOR_CN)
    corrupted[80] = "A" if corrupted[80] != "A" else "B"
    try:
        trae_storage.decrypt_tc_value("".join(corrupted))
        check("损坏凭据被校验拦下", False, "竟然解出来了")
    except ValueError as exc:
        check("损坏凭据被校验拦下", "校验失败" in str(exc) or "解密失败" in str(exc) or "填充" in str(exc), str(exc))


def test_trae_storage_import() -> None:
    """从粘贴的客户端全局存储里解出可入库凭据。"""
    content = json.dumps({trae_storage.AUTH_KEY: STORAGE_VECTOR_CN, "other": "noise"})
    creds = trae_storage.credentials_from_storage(content)
    check(
        "解出 token",
        creds["jwt"].startswith("Cloud-IDE-JWT ")
        and trae_identity.jwt_parse(creds["jwt"])["user_id"] == "99001"
    )
    eq("解出 refresh", creds["refreshToken"], "demo-refresh-token-cn")
    eq("解出 userId", creds["userId"], "9002")
    eq("解出 host", creds["host"], "api.trae.com.cn")

    # 少数版本直接存明文 JSON
    plain = json.dumps({trae_storage.AUTH_KEY: {"token": "plain-token", "userId": "1"}})
    eq("明文形态也能取到", trae_storage.credentials_from_storage(plain)["jwt"], "Cloud-IDE-JWT plain-token")

    # 凭据自带的身份线索（多账号区分用）
    identified = json.dumps(
        {trae_storage.AUTH_KEY: {"token": "tok", "userId": "1", "userName": "小明", "email": "m@x.y"}}
    )
    creds2 = trae_storage.credentials_from_storage(identified)
    eq("凭据昵称提取", creds2["userName"], "小明")
    eq("凭据邮箱提取", creds2["email"], "m@x.y")
    anonymous = trae_storage.credentials_from_storage(
        json.dumps({trae_storage.AUTH_KEY: {"token": "tok", "userId": "1"}})
    )
    eq("无身份字段时为空串", (anonymous["userName"], anonymous["email"]), ("", ""))

    # 缺键 / 空内容 / 非 JSON 都要给出可读原因
    for name, payload in (
        ("缺少凭据键", json.dumps({"other": "1"})),
        ("空内容", ""),
        ("非 JSON", "not json at all"),
        ("凭据为空", json.dumps({trae_storage.AUTH_KEY: "  "})),
    ):
        try:
            trae_storage.credentials_from_storage(payload)
            check(f"{name}被拒绝", False, "竟然通过了")
        except ValueError as exc:
            check(f"{name}被拒绝", bool(str(exc).strip()))


# ===========================================================================
# 计费统计解析（官方真实字段形态回归：字段名一改就要能立刻发现）
# ===========================================================================


def _fake_http(routes: dict):
    """按 URL 后缀匹配的假 HTTP 层；未命中的请求直接断言失败，暴露漏配。"""

    def handler(url, method, body, headers):
        for suffix, response in routes.items():
            if url.endswith(suffix):
                return response(body) if callable(response) else response
        raise AssertionError(f"未预期的请求：{url}")

    return handler


def _with_http(routes: dict, fn):
    original = util.http_json
    util.http_json = _fake_http(routes)
    try:
        return fn()
    finally:
        util.http_json = original


def _stamp(ts_ms: int) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _wb_account() -> dict:
    return {"id": "wb-1", "uid": "u-1", "access_token": "token", "domain": "www.codebuddy.cn"}


def test_wb_stats_credits_real_shape() -> None:
    """总览 Packages + 明细 Accounts 三种容量命名都要能解析出来。"""
    now_ms = util.now_ms()
    expire = now_ms + 20 * 86400 * 1000
    routes = {
        "/get-user-resource-summary": {
            "code": 0,
            "data": {
                "Packages": [
                    {
                        "PackageCode": "sum-1",
                        "PackageName": "总览包",
                        "CycleTotalCapacity": "4485",
                        "CycleUsedCapacity": "2156.70999737",
                        "CycleRemainCapacity": "2328.29000263",
                    }
                ]
            },
        },
        "/get-user-resource-paid-packages": {
            "code": 0,
            "data": {
                "Accounts": [
                    {
                        "PackageCode": "paid-1",
                        "PackageName": "会员月包",
                        "CycleCapacitySizePrecise": "500",
                        "CycleCapacityRemainPrecise": "400",
                        "DeductionEndTime": expire,
                    }
                ]
            },
        },
        "/get-user-resource-free-packages": {"code": 0, "data": {"Accounts": []}},
    }
    wb_stats.stats_invalidate()
    result = _with_http(routes, lambda: wb_stats.fetch_credits(_wb_account(), force=True))
    wb_stats.stats_invalidate()

    check("WB 积分：查询成功", result.get("ok") is True, str(result))
    eq("WB 积分：余额 = 总览剩余 + 明细剩余", result.get("balance"), 2728.29)
    packages = {p["name"]: p for p in result.get("packages") or []}
    check("WB 积分：总览包有被解析（不再读空）", "总览包" in packages, str(list(packages)))
    eq("WB 积分：总览包剩余", packages.get("总览包", {}).get("remaining"), 2328.29)
    eq("WB 积分：明细包到期时间", packages.get("会员月包", {}).get("expireTs"), expire)
    eq("WB 积分：明细包类型", packages.get("会员月包", {}).get("kind"), "付费")


def test_wb_stats_credits_legacy_fallback() -> None:
    """三路接口全失败时回落单接口形态，而不是直接报错。"""
    routes = {
        "/get-user-resource-summary": {"code": 500, "message": "boom"},
        "/get-user-resource-paid-packages": {"code": 500, "message": "boom"},
        "/get-user-resource-free-packages": {"code": 500, "message": "boom"},
        "/billing/meter/get-user-resource": {
            "code": 0,
            "data": {
                "Accounts": [
                    {
                        "PackageCode": "legacy",
                        "PackageName": "通用包",
                        "CapacitySize": 100,
                        "CapacityRemain": 60,
                    }
                ]
            },
        },
    }
    wb_stats.stats_invalidate()
    result = _with_http(routes, lambda: wb_stats.fetch_credits(_wb_account(), force=True))
    wb_stats.stats_invalidate()
    check("WB 积分：三路失败后回落旧接口", result.get("ok") is True, str(result))
    eq("WB 积分：回落后的余额", result.get("balance"), 60.0)


def test_wb_stats_usage_scan_and_tokens() -> None:
    """用量必须按时间推进连续扫描；行内的 token 字段要能识别。"""
    original_size = wb_stats.USAGE_PAGE_SIZE
    wb_stats.USAGE_PAGE_SIZE = 2
    now_ms = util.now_ms()
    calls = {"n": 0}

    def route(body):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "code": 0,
                "data": {
                    "total": 2,
                    "data": [
                        {
                            "requestId": "r1",
                            "credit": "1.5",
                            "model": "m-a",
                            "client": "ide",
                            "requestTime": _stamp(now_ms - 3600_000),
                            "inputTokens": 1000,
                            "outputTokens": 200,
                            "cacheReadTokens": 800,
                        },
                        {
                            "requestId": "r2",
                            "credit": "0.5",
                            "model": "m-b",
                            "client": "ide",
                            "requestTime": _stamp(now_ms - 1800_000),
                            "inputTokens": 500,
                            "outputTokens": 100,
                            "cacheReadTokens": 0,
                        },
                    ],
                },
            }
        return {"code": 0, "data": {"total": 0, "data": []}}

    wb_stats.stats_invalidate()
    try:
        result = _with_http({"/get-user-request-usage": route}, lambda: wb_stats.fetch_usage(_wb_account(), 7, force=True))
    finally:
        wb_stats.USAGE_PAGE_SIZE = original_size
        wb_stats.stats_invalidate()

    check("WB 用量：连续扫描取到全部请求", result.get("ok") is True and result.get("totalCalls") == 2, str(result))
    check("WB 用量：Token 维度被识别", result.get("tokenAvailable") is True)
    eq("WB 用量：输入 Token 合计", (result.get("tokenTotals") or {}).get("input"), 1500)
    eq("WB 用量：输出 Token 合计", (result.get("tokenTotals") or {}).get("output"), 300)
    check("WB 用量：请求明细已产出", len(result.get("requests") or []) == 2)
    check("WB 用量：第二轮扫描被触发", calls["n"] >= 2, f"calls={calls['n']}")


def test_trae_stats_credits_real_shape() -> None:
    """Trae 积分包：剩余 = credits_limit - usage.credits_amount，过期包不入余额。"""
    now = int(time.time())
    response = {
        "user_entitlement_pack_list": [
            {
                "group_name": "签到奖励",
                "expire_time": now + 5 * 86400,
                "usage": {"credits_amount": 10},
                "entitlement_base_info": {"product_id": 209, "quota": {"credits_limit": 30}},
            },
            {
                "group_name": "会员积分",
                "display_desc": "会员 Lite 连续包月",
                "expire_time": now + 20 * 86400,
                "entitlement_base_info": {
                    "product_id": 1,
                    "quota": {"credits_limit": 500},
                    "charge_amount": 20,
                    "end_time": now + 20 * 86400,
                },
            },
            {
                "group_name": "已过期包",
                "expire_time": now - 3600,
                "entitlement_base_info": {"product_id": 1, "quota": {"credits_limit": 100}},
            },
        ]
    }
    trae_stats.stats_invalidate()
    result = _with_http({"/ide_user_ent_usage": response}, lambda: trae_stats.fetch_credits({"id": "t-1", "jwt": "j"}, force=True))
    trae_stats.stats_invalidate()

    check("Trae 积分：查询成功", result.get("ok") is True, str(result))
    eq("Trae 积分：余额只含可用包", result.get("balance"), 520.0)
    eq("Trae 积分：Work 积分单列", result.get("work"), 20.0)
    eq("Trae 积分：通用积分单列", result.get("general"), 500.0)
    check("Trae 积分：过期包被过滤", len(result.get("packages") or []) == 2, str(result.get("packages")))
    eq(
        "Trae 积分：来源标签归一",
        sorted(p["name"] for p in result.get("packages") or []),
        ["会员/购买", "每日签到"],
    )
    check("Trae 积分：会员到期时间", result.get("membershipExpireTs") == (now + 20 * 86400) * 1000)


def test_trae_stats_usage_page_param_error() -> None:
    """分页/区间参数被拒时要给出可读提示，不能只说「查询失败」。"""
    trae_stats.stats_invalidate()
    result = _with_http(
        {"/query_user_usage_group_by_session": {"code": 9004, "message": "param invalid"}},
        lambda: trae_stats.fetch_usage({"id": "t-1", "jwt": "j"}, 7, force=True),
    )
    trae_stats.stats_invalidate()
    check("Trae 用量：参数错误可读", result.get("ok") is False and "参数" in str(result.get("error")), str(result))


def test_version_matches_manifest() -> None:
    """应用内版本与打包 manifest 必须一致（曾经漏升导致「关于」显示旧版本）。"""
    from core import APP_VERSION

    root = os.path.dirname(APP_DIR)
    version_line = ""
    with open(os.path.join(root, "manifest"), encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("version"):
                version_line = line.split("=", 1)[1].strip()
                break
    eq("版本号：APP_VERSION 与 manifest 同步", APP_VERSION, version_line)


def main() -> int:
    for fn in (
        test_variant,
        test_util,
        test_wb_checkin,
        test_trae_jwt,
        test_trae_device,
        test_trae_classify,
        test_trae_reward,
        test_trae_credits_fallback,
        test_trae_envelope,
        test_trae_humanize,
        test_trae_cooldown,
        test_wb_receipt,
        test_trae_cycle_rows,
        test_store,
        test_oauth_pkce,
        test_oauth_host_allowlist,
        test_oauth_find_token,
        test_oauth_volcano_error,
        test_oauth_wb_domain,
        test_oauth_trae_start,
        test_oauth_trae_callback_direct,
        test_oauth_parse_callback_params,
        test_oauth_trae_precheck,
        test_oauth_trae_exchange_errors,
        test_trae_storage_decrypt,
        test_trae_storage_import,
        test_wb_stats_credits_real_shape,
        test_wb_stats_credits_legacy_fallback,
        test_wb_stats_usage_scan_and_tokens,
        test_trae_stats_credits_real_shape,
        test_trae_stats_usage_page_param_error,
        test_version_matches_manifest,
    ):
        fn()
    print(f"\n单元测试：{PASSED} 通过 / {FAILED} 失败")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
