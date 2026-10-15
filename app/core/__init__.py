#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QDAssistant 核心层。

    util           通用工具（时间 / 文件 / HTTP）
    store          持久化（配置 / 账号 / 日志 / Trae 侧写）
    variant        WorkBuddy 档位单一事实来源
    wb_accounts    WorkBuddy 鉴权头 / token 刷新
    wb_checkin     WorkBuddy 签到引擎
    trae_identity  Trae JWT 解析 / 设备指纹派生
    trae_checkin   Trae 签到引擎
    oauth          两平台 OAuth 登录采集
    scheduler      后台调度循环

两个引擎彼此独立：账号按 `platform` 分库（`workbuddy` / `trae`），
调度器分别驱动，互不阻塞。
"""

__all__ = [
    "util",
    "store",
    "variant",
    "wb_accounts",
    "wb_checkin",
    "trae_identity",
    "trae_checkin",
    "oauth",
    "scheduler",
]

APP_NAME = "QDAssistant"
APP_TITLE = "签到助手"
APP_VERSION = "1.0.0"

# 作者 / 开发者信息（界面「关于」与 HTTP Server 头统一使用）
APP_AUTHOR = "Misite齊"
APP_AUTHOR_URL = "https://github.com/MisiteQ"
APP_REPO_URL = "https://github.com/MisiteQ/qdassistant"
