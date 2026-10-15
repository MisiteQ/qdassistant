#!/usr/bin/env python3
"""往临时数据目录灌入演示数据，用于本地预览界面（不会触碰真实数据）。

用法: python tools/seed_demo.py [数据目录]
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from core import store, util  # noqa: E402


def fake_jwt(user_id: str, hours: float = 300.0) -> str:
    """构造一个结构上合法的演示 JWT（签名段为占位）。"""
    import base64
    import json

    def b64(obj) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = {"alg": "RS256", "typ": "JWT"}
    now = int(time.time())
    # 与真实 Trae JWT 结构一致：user id 取 data.id，其次 auth_id / sub
    payload = {
        "data": {"id": user_id},
        "auth_id": user_id,
        "sub": user_id,
        "exp": now + int(hours * 3600),
        "iat": now - 60,
        "iss": "trae",
    }
    return f"Cloud-IDE-JWT {b64(header)}.{b64(payload)}.ZmFrZS1zaWduYXR1cmUtZm9yLWRlbW8"


def main() -> int:
    data_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".demo-data")
    store.set_data_dir(os.path.abspath(data_dir))

    store.save_config(
        {
            "host": "0.0.0.0",
            "port": 8790,
            "checkin_start": "08:30",
            "checkin_end": "10:00",
            "timezone": "Asia/Shanghai",
            "workbuddy": {"enabled": True, "auto_refresh": True},
            "trae": {"enabled": True, "gap_secs": 5},
        }
    )

    # WorkBuddy 账号
    store.upsert_account(
        store.PLATFORM_WORKBUDDY,
        {
            "id": "wb-demo-1",
            "name": "主号 · WorkBuddy",
            "nickname": "主号",
            "email": "me@example.com",
            "uid": "10001",
            "access_token": "wb_demo_access_token_0000000000000000",
            "refresh_token": "wb_demo_refresh_token_000000000000000",
            "supports_checkin": True,
            "needs_relogin": False,
        },
    )
    store.upsert_account(
        store.PLATFORM_WORKBUDDY,
        {
            "id": "wb-demo-2",
            "name": "小号 · WorkBuddy",
            "nickname": "小号",
            "email": "alt@example.com",
            "uid": "10002",
            "access_token": "wb_demo_access_token_1111111111111111",
            "refresh_token": "wb_demo_refresh_token_111111111111111",
            "supports_checkin": False,
            "needs_relogin": True,
        },
    )

    # Trae 账号
    store.upsert_account(store.PLATFORM_TRAE, {"id": "trae-demo-1", "name": "主号 · Trae", "jwt": fake_jwt("10000001", 300)})
    store.upsert_account(store.PLATFORM_TRAE, {"id": "trae-demo-2", "name": "备用 · Trae", "jwt": fake_jwt("10000002", 6)})

    # 近期签到日志
    base = util.now_ms()
    for offset, (platform, account, status, detail, credits) in enumerate(
        [
            ("workbuddy", "wb-demo-1", "success", "签到成功 +12 积分", 12),
            ("trae", "10000001", "success", "领取成功 +15 积分", 15),
            ("workbuddy", "wb-demo-2", "error", "账号需重新登录", 0),
            ("trae", "10000002", "already", "今日已签到", 0),
            ("workbuddy", "wb-demo-1", "success", "签到成功 +12 积分", 12),
            ("trae", "10000001", "success", "领取成功 +15 积分", 15),
        ]
    ):
        store.add_checkin_log(
            {
                "ts": base - offset * 3600_000 * 20,
                "platform": platform,
                "account": account,
                "status": status,
                "detail": detail,
                "credits": credits,
            }
        )

    print(f"演示数据已写入: {store.data_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
