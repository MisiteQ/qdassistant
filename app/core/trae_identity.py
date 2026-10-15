#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trae 身份解析：JWT 解码 + 设备指纹派生。

JWT 只做本地解析（不校验签名），用于取 `data.id` 与 `exp` 供展示与过期告警。
设备指纹按 uid 确定性派生（SHA-256 种子流），保证对同一账号生成完全一致的
设备标识——2026-09 起 Trae 服务端会校验设备指纹，仅带 authorization 会 401。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

# JWT 剩余有效期低于该值（小时）时写入告警（与 EXPIRY_WARN_HOURS 保持一致）
EXPIRY_WARN_HOURS = 24.0


def jwt_parse(jwt_full: str) -> dict:
    """解析 JWT，返回 `{"user_id", "exp_hours", "exp_timestamp", "name", "email"}`。

    支持 `Cloud-IDE-JWT <token>` / `Bearer <token>` 前缀与裸 token；
    解析失败时各字段均为 None（不抛异常）。
    `name` / `email` 是展示用的身份线索（多账号时帮用户分清是谁），
    属于"有则带出"，不同版本的 JWT 不一定都有。
    """
    empty = {"user_id": None, "exp_hours": None, "exp_timestamp": None, "name": None, "email": None}
    if not isinstance(jwt_full, str):
        return empty
    trimmed = jwt_full.strip()
    for prefix in ("Cloud-IDE-JWT ", "Bearer "):
        if trimmed.startswith(prefix):
            trimmed = trimmed[len(prefix) :].strip()
            break

    parts = trimmed.split(".")
    if len(parts) < 2:
        return empty

    payload_b64 = parts[1].rstrip("=")
    # base64url 无填充；补齐 padding 后用标准解码器
    padding = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return empty
    if not isinstance(payload, dict):
        return empty

    # user_id：data.id > auth_id > sub
    user_id = None
    data = payload.get("data")
    if isinstance(data, dict):
        value = data.get("id")
        if isinstance(value, str) and value:
            user_id = value
        elif isinstance(value, int):
            user_id = str(value)
    if user_id is None:
        for key in ("auth_id", "sub"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                user_id = value
                break

    exp = payload.get("exp")
    exp_timestamp = None
    if isinstance(exp, bool):
        exp_timestamp = None
    elif isinstance(exp, (int, float)):
        exp_timestamp = int(exp)
    elif isinstance(exp, str):
        try:
            exp_timestamp = int(float(exp.strip()))
        except ValueError:
            exp_timestamp = None

    exp_hours = None
    if exp_timestamp is not None:
        exp_hours = (exp_timestamp - int(time.time())) / 3600.0

    # 展示用身份线索：字段名随客户端版本有差异，逐个候选兜底
    data_extra = data if isinstance(data, dict) else {}

    def _first_str(*values) -> str | None:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    name = _first_str(
        payload.get("name"),
        payload.get("nickname"),
        payload.get("userName"),
        payload.get("user_name"),
        payload.get("displayName"),
        data_extra.get("name"),
        data_extra.get("nickname"),
        data_extra.get("userName"),
    )
    email = _first_str(payload.get("email"), payload.get("mail"), data_extra.get("email"))

    return {
        "user_id": user_id,
        "exp_hours": exp_hours,
        "exp_timestamp": exp_timestamp,
        "name": name,
        "email": email,
    }


def jwt_status(exp_hours: float | None) -> str:
    """由 exp 剩余小时数推导状态：>24 ok / <=24 && >0 warn / <=0 expired。"""
    if exp_hours is None:
        return "unknown"
    if exp_hours > 24.0:
        return "ok"
    if exp_hours > 0.0:
        return "warn"
    return "expired"


def _seeded_stream(seed: str, salt: str, nbytes: int) -> bytes:
    """按 uid 确定性派生字节流：`SHA-256("salt:uid" + 4 字节大端计数器)` 级联。"""
    prefix = f"{salt}:{seed}".encode("utf-8")
    out = bytearray()
    counter = 0
    while len(out) < nbytes:
        digest = hashlib.sha256(prefix + counter.to_bytes(4, "big")).digest()
        out.extend(digest)
        counter = (counter + 1) & 0xFFFFFFFF
    return bytes(out[:nbytes])


def derive_device(uid: str) -> dict:
    """由 uid 派生设备三元组：`device_id` / `market_user_id` / `session_id`。"""
    device_id = "".join(str(byte % 10) for byte in _seeded_stream(uid, "devid", 15))
    session_id = "".join(f"{byte:02x}" for byte in _seeded_stream(uid, "sess", 32))

    market = bytearray(_seeded_stream(uid, "market", 16))
    market[6] = (market[6] & 0x0F) | 0x40  # UUID v4 版本位
    market[8] = (market[8] & 0x3F) | 0x80  # UUID v4 变体位
    hexed = [f"{byte:02x}" for byte in market]
    market_user_id = (
        f"{hexed[0]}{hexed[1]}{hexed[2]}{hexed[3]}-"
        f"{hexed[4]}{hexed[5]}-{hexed[6]}{hexed[7]}-"
        f"{hexed[8]}{hexed[9]}-{hexed[10]}{hexed[11]}{hexed[12]}{hexed[13]}{hexed[14]}{hexed[15]}"
    )
    return {
        "device_id": device_id,
        "market_user_id": market_user_id,
        "session_id": session_id,
    }


def get_device_for(uid: str) -> dict:
    """解析账号设备标识：`device_map.json` 已有条目优先，否则派生并落盘共享。

    已有条目通常是代理捕获 / 切换流程写入的，与新 JWT 的设备指纹校验匹配，
    因此优先沿用；没有才按 uid 确定性派生（与脚本同算法）。
    """
    from . import store

    mapping = store.load_device_map()
    entry = mapping.get(uid)
    if isinstance(entry, dict) and entry.get("device_id"):
        return {
            "device_id": entry.get("device_id", ""),
            "market_user_id": entry.get("market_user_id"),
            "session_id": entry.get("session_id"),
        }
    device = derive_device(uid)
    mapping[uid] = device
    store.save_device_map(mapping)
    return device
