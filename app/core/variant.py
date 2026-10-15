#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 档位（国内版 / 国际版）的单一事实来源。

档位差异（域名、接口前缀、能力声明）只允许从这里取，其它模块不得再出现
档位相关字面量。

缺省档位为 `Cn`：历史账号没有 `variant` 字段时一律按国内版处理。
"""

from __future__ import annotations

# 国内版 API 基址
CN_API_ENDPOINT = "https://www.codebuddy.cn"
# 国际版 API 基址
AI_API_ENDPOINT = "https://www.workbuddy.ai"
# 两档位共用接口前缀（WORKBUDDY_API_PREFIX）
API_PREFIX = "/v2/plugin"
# 国内版 billing 接口前缀（CHECKIN_API_PREFIX）
CHECKIN_API_PREFIX = "/v2/billing/meter"
# 国际版 billing 接口前缀（实测形态）
AI_BILLING_PREFIX = "/billing/meter"

# 账号 / 响应 `domain` 判据：该后缀视为国际版
AI_DOMAIN_SUFFIX = ".workbuddy.ai"

CN = "cn"
AI = "ai"


def parse(raw) -> str:
    """解析档位字符串；未知 / 空 / 缺省一律回落国内版（向后兼容）。"""
    if not isinstance(raw, str):
        return CN
    value = raw.strip().lower()
    if value in ("ai", "intl", "global"):
        return AI
    return CN


def from_account(account: dict) -> str:
    """从账号记录判定档位：显式 `variant` 字段优先，其次 `domain` 后缀兜底。"""
    if isinstance(account, dict):
        explicit = account.get("variant")
        if isinstance(explicit, str) and explicit.strip():
            return parse(explicit)
        domain = account.get("domain")
        if isinstance(domain, str) and domain.strip().lower().endswith(AI_DOMAIN_SUFFIX):
            return AI
    return CN


def api_endpoint(variant: str) -> str:
    return AI_API_ENDPOINT if variant == AI else CN_API_ENDPOINT


def api_prefix(_variant: str) -> str:
    """设备码流程接口前缀（两档位相同）。"""
    return API_PREFIX


def billing_paths(variant: str, path: str) -> list[str]:
    """billing 接口路径候选（顺序即回落顺序）。

    国内版只返回给定路径本身；国际版实测走 `/billing/meter/...`，故先试去掉
    `/v2` 的形态，再回落 `/v2/...`。**只有 HTTP 404 才允许换下一个候选**
    （401/10085/传输错误都不是路径问题）。
    """
    if variant != AI:
        return [path]
    if path.startswith(CHECKIN_API_PREFIX):
        primary = AI_BILLING_PREFIX + path[len(CHECKIN_API_PREFIX) :]
    else:
        primary = path
    fallback = f"/v2{primary}"
    if fallback == primary:
        return [primary]
    return [primary, fallback]


def supports_checkin(variant: str) -> bool:
    """该档位是否支持签到，**同时门控请求与待签到集合**。

    国际版没有签到接口：签到链路在发起任何请求前一律跳过，既不发请求也不写日志。
    """
    return variant != AI
