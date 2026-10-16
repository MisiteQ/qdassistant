#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务端冒烟测试：真实起一个 HTTP 服务，走完主要接口。

用法：
    python3 tests/test_smoke.py

不依赖第三方库；测试用独立临时数据目录，不污染真实数据。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(os.path.dirname(HERE), "app")
sys.path.insert(0, APP_DIR)
sys.path.insert(0, HERE)

TMP_DIR = tempfile.mkdtemp(prefix="qd-smoke-")
os.environ["QD_DATA_DIR"] = TMP_DIR

import server  # noqa: E402
from core import store  # noqa: E402

# 冒烟测试不起后台调度线程（避免真实网络请求）
PORT = 18990
BASE = f"http://127.0.0.1:{PORT}"

RESULTS: list[tuple[str, bool, str]] = []


# 客户端凭据样本（与单元测试同源，由权威 AES 实现加密生成）
STORAGE_VECTOR_CN = (
    "dGMFEAAA/yCCqniuqAony0+5HwNQFTcCwW3OeQp38LsL+/aJmXeZ5CZbHJSStQeU64PHWzHPnetQ"
    "LOUpTPS3lFc6dRNdvFB8aahatkNeRADMfX6dqMQxvXrQQvrTsISQDylKe7TiWlxizGYa3P5K66qz"
    "MoZWQTYZUxT8no6stC7xoKK8dcEGNAy2HzmMiiqH+KeCJKXorMWvBjN+PHL3eIvCrFjyuuJ02JQi"
    "I+3ZzUbwdEa30+Bu4iwliP1azIB70VTrJ/Bb8O2A2n+IP1I7bqZZ7akfeFVBJhETa+T1lIVB5RQ3"
    "v6z/cN6AdFokjxyCYytWFA7HIazdFnIX5rSW5364i1/n5/ZqYz/CM8IOMa3Zq2b1pSHPrrNWAJ3V"
    "R3XHJjTxwni3aRPZgjZ5AvT1sXQMfWl/m/pg838KPCN0iRwY9BLYNCw="
)


def request(path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("X-QD-Request", "1")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    flag = "PASS" if condition else "FAIL"
    print(f"[{flag}] {name}" + (f" — {detail}" if detail and not condition else ""))


def main() -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.4)

    try:
        # 1. 服务状态
        status = request("/api/status")
        check("GET /api/status 返回应用名", status.get("app") == "QDAssistant")
        check("GET /api/status 返回数据目录", status.get("dataDir") == TMP_DIR)

        # 1b. 首屏聚合接口：一次返回全部初始数据。
        #     真机上的统一网关并发转发不可靠 —— 首屏并发发出的多个接口会一起
        #     悬住（面板停在加载态），因此首屏必须收敛成单个请求。
        boot = request("/api/bootstrap")
        check("GET /api/bootstrap 含 status 段", (boot.get("status") or {}).get("app") == "QDAssistant")
        check("GET /api/bootstrap 含 checkin 段", isinstance(boot.get("checkin"), dict))
        check("GET /api/bootstrap 含 accounts 段（脱敏视图）", isinstance(boot.get("accounts"), dict))
        check("GET /api/bootstrap 含 config 段", isinstance(boot.get("config"), dict))
        stats_seg = boot.get("stats") or {}
        check("GET /api/bootstrap 统计段只读缓存（首装为空）", (stats_seg.get("workbuddy") or {}).get("credits") is None)
        check("GET /api/bootstrap 默认统计天数 30", stats_seg.get("days") == 30)
        check("GET /api/bootstrap 天数参数生效", (request("/api/bootstrap?days=7").get("stats") or {}).get("days") == 7)

        # 2. 静态资源
        for path, needle in (("/", "签到助手"), ("/app.js", "X-QD-Request"), ("/style.css", "--accent")):
            with urllib.request.urlopen(BASE + path, timeout=10) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            check(f"静态资源 {path} 可访问", needle in text)

        # 3. 添加 Trae 账号（构造一个带 exp 的假 JWT）
        import base64

        payload = {"data": {"id": "999001"}, "exp": int(time.time()) + 3600 * 24 * 30}
        seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        fake_jwt = f"header.{seg}.signature"
        added = request(
            "/api/accounts",
            "POST",
            {"platform": "trae", "jwt": fake_jwt, "name": "冒烟账号"},
        )
        check("POST /api/accounts 添加 Trae 账号", added.get("ok") is True, str(added))
        check("Trae 账号解析出 user id", (added.get("account") or {}).get("userId") == "999001")

        # 4. 重复添加同一 user_id 应更新而非新增
        added2 = request("/api/accounts", "POST", {"platform": "trae", "jwt": fake_jwt})
        accounts = request("/api/accounts?platform=trae")
        check("同一 user_id 不重复入库", len(accounts["accounts"]) == 1, str(len(accounts["accounts"])))
        check("重复添加被判定为更新", added2.get("ok") is True)

        # 5. 空 JWT 应被拒绝
        try:
            request("/api/accounts", "POST", {"platform": "trae", "jwt": "  "})
            check("空 JWT 被拒绝", False, "未返回错误")
        except urllib.error.HTTPError:
            check("空 JWT 被拒绝", True)

        # 6. WorkBuddy 账号
        wb = request(
            "/api/accounts",
            "POST",
            {"platform": "workbuddy", "access_token": "smoke-token-1234567890", "nickname": "WB测试"},
        )
        check("POST /api/accounts 添加 WorkBuddy 账号", wb.get("ok") is True)
        masked = (wb.get("account") or {}).get("accessTokenMasked", "")
        check("WorkBuddy token 仅保留首尾各 4 位", masked == "smok" + "*" * 8 + "7890", masked)
        check("WorkBuddy token 中段被打码", "*" in (wb.get("account") or {}).get("accessTokenMasked", ""))

        # 7. 配置读写
        cfg = request("/api/config")
        check("GET /api/config 返回配置", "config" in cfg)
        saved = request("/api/config", "POST", {"recovery_interval_minutes": 45})
        check("POST /api/config 保存生效", saved["config"]["recovery_interval_minutes"] == 45)

        # 8. 签到状态
        st = request("/api/checkin/status")
        check("GET /api/checkin/status 覆盖两个平台", "workbuddy" in st and "trae" in st)
        check("待签到统计正确", st["workbuddy"]["total"] == 1 and st["trae"]["total"] == 1)

        # 9. CSRF 闸：不带 X-QD-Request 的 POST 必须被拒
        req = urllib.request.Request(
            BASE + "/api/accounts",
            data=json.dumps({"platform": "trae", "jwt": fake_jwt}).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=10)
            check("缺少 X-QD-Request 的 POST 被拒", False, "竟然通过了")
        except urllib.error.HTTPError as exc:
            check("缺少 X-QD-Request 的 POST 被拒", exc.code == 403, f"状态码 {exc.code}")

        # 10. 删除账号
        account_id = (wb.get("account") or {}).get("id")
        deleted = request("/api/accounts/delete", "POST", {"platform": "workbuddy", "id": account_id})
        check("POST /api/accounts/delete 删除账号", deleted.get("ok") is True)
        left = request("/api/accounts?platform=workbuddy")
        check("删除后账号列表为空", len(left["accounts"]) == 0)

        # 11. 日志接口
        logs = request("/api/checkin/logs")
        check("GET /api/checkin/logs 可用", "logs" in logs)
        applog = request("/api/logs")
        check("GET /api/logs 可用", "lines" in applog)

        # 12. 配置导出 / 导入：账号搬家往返（换 NAS 场景）
        request(
            "/api/accounts",
            "POST",
            {"platform": "workbuddy", "access_token": "export-token-abcdefghij", "nickname": "搬家测试号"},
        )
        doc = request("/api/export")
        check("GET /api/export 返回导出文档", doc.get("schema") == "qdassistant.config/v1", str(doc.get("schema")))
        check(
            "导出统计账号数",
            doc.get("counts") == {"workbuddy": 1, "trae": 1},
            str(doc.get("counts")),
        )
        exported_accounts = doc.get("accounts") or {}
        wb_row = (exported_accounts.get("workbuddy") or [{}])[0]
        trae_row = (exported_accounts.get("trae") or [{}])[0]
        check("导出保留 WorkBuddy 凭据原文", wb_row.get("access_token") == "export-token-abcdefghij")
        check("导出保留 Trae JWT 原文", bool(trae_row.get("jwt")), str(trae_row.get("jwt"))[:14])
        policy = doc.get("policy") or {}
        check(
            "导出不含机器相关字段（地址 / 端口 / 口令）",
            not ({"host", "port", "access_password"} & set(policy)),
            str(sorted(policy)),
        )

        # 清空本机账号，模拟「新设备」
        for platform in ("workbuddy", "trae"):
            for acc in request(f"/api/accounts?platform={platform}")["accounts"]:
                request("/api/accounts/delete", "POST", {"platform": platform, "id": acc["id"]})
        emptied = request("/api/accounts")
        check("清空后账号为空", emptied["accounts"] == [])

        restored = request("/api/import", "POST", {"document": doc})
        check("POST /api/import 导入成功", restored.get("ok") is True, str(restored))
        check(
            "导入新增两个账号",
            restored.get("added") == {"workbuddy": 1, "trae": 1},
            str(restored.get("added")),
        )
        back = request("/api/accounts")
        check("导入后账号数恢复", len(back["accounts"]) == 2, str(len(back["accounts"])))
        wb_back = request("/api/accounts?platform=workbuddy")["accounts"]
        check("导入后 WorkBuddy 凭据可用", bool(wb_back) and wb_back[0]["hasRefreshToken"] is False, str(wb_back))
        trae_back = request("/api/accounts?platform=trae")["accounts"]
        check("导入后 Trae 账号可识别", bool(trae_back) and trae_back[0].get("userId") == "999001", str(trae_back))

        # 同一份文件再导入一次：必须就地更新而不是翻倍
        second = request("/api/import", "POST", {"document": doc})
        check(
            "重复导入就地更新",
            second.get("added") == {"workbuddy": 0, "trae": 0}
            and second.get("updated") == {"workbuddy": 1, "trae": 1},
            str(second),
        )
        check("重复导入不新增账号", len(request("/api/accounts")["accounts"]) == 2)

        # 默认不动本机策略，显式要求才应用
        check(
            "默认不改动本机签到策略",
            request("/api/config")["config"]["recovery_interval_minutes"] == 45,
        )
        applied = request("/api/import", "POST", {"document": doc, "applyPolicy": True})
        check("显式要求才应用策略", applied.get("policyApplied") is True, str(applied.get("policyApplied")))

        try:
            request("/api/import", "POST", {"document": {"hello": "world"}})
            check("无 accounts 段的文件被拒", False, "竟然通过了")
        except urllib.error.HTTPError as exc:
            check("无 accounts 段的文件被拒", exc.code == 400, f"状态码 {exc.code}")

        # 收尾：恢复成「只有 1 个 Trae 账号」，与后续步骤的前置状态一致
        for acc in request("/api/accounts?platform=workbuddy")["accounts"]:
            request("/api/accounts/delete", "POST", {"platform": "workbuddy", "id": acc["id"]})
        check("收尾清空 WorkBuddy 账号", request("/api/accounts?platform=workbuddy")["accounts"] == [])

        # 13. Trae OAuth：登录链接生成 + 回调即时校验（纯本地判定，不发外部请求）
        started = request("/api/oauth/trae/start", "POST", {})
        check("POST /api/oauth/trae/start 返回登录链接", str(started.get("url", "")).startswith("https://www.trae.cn/authorization?"))
        check("start 返回 AuthCode 有效期（秒）", started.get("authCodeTtl") == 600, str(started.get("authCodeTtl")))

        fresh = json.dumps({"AuthCode": "smoke-code", "ExpireDuration": 600000, "CreateAt": int(time.time() * 1000)})
        probed = request(
            "/api/oauth/trae/callback/check",
            "POST",
            {"callbackUrl": f"http://127.0.0.1:17388/authorize?authCodeInfo={urllib.parse.quote(fresh)}"},
        )
        check("回调即时校验返回 check 段", "check" in probed)
        check("新鲜回调判定可用", probed["check"].get("ok") is True, str(probed["check"]))

        stale = json.dumps({"AuthCode": "smoke-code", "ExpireDuration": 600000, "CreateAt": int((time.time() - 900) * 1000)})
        probed_old = request(
            "/api/oauth/trae/callback/check",
            "POST",
            {"callbackUrl": f"http://127.0.0.1:17388/authorize?authCodeInfo={urllib.parse.quote(stale)}"},
        )
        check("过期回调判定 expired", probed_old["check"].get("expired") is True)

        # 过期码提交不应打出交换异常：以 200 + expired 标记返回
        submitted = request(
            "/api/oauth/trae/callback",
            "POST",
            {"callbackUrl": f"http://127.0.0.1:17388/authorize?authCodeInfo={urllib.parse.quote(stale)}"},
        )
        check("过期回调提交返回 expired 标记", submitted.get("expired") is True, str(submitted))
        check("过期回调提交给出可读提示", "重新" in str(submitted.get("error", "")))

        # 13. 客户端凭据导入（NAS 上 OAuth 走不通时的主路径）
        storage_content = json.dumps({"iCubeAuthInfo://icube.cloudide": STORAGE_VECTOR_CN, "other": "x"})
        imported = request(
            "/api/accounts/import-storage",
            "POST",
            {"content": storage_content, "name": "冒烟导入号"},
        )
        check("POST /api/accounts/import-storage 导入成功", imported.get("ok") is True, str(imported))
        check("导入账号解析出 user id", (imported.get("account") or {}).get("userId") == "99001", str(imported.get("account")))
        check("自定义备注名生效", (imported.get("account") or {}).get("name") == "冒烟导入号", str(imported.get("account", {}).get("name")))
        check("导入带回 refresh_token", imported.get("hasRefreshToken") is True)

        again = request("/api/accounts/import-storage", "POST", {"content": storage_content})
        check("重复导入保留已有备注名", (again.get("account") or {}).get("name") == "冒烟导入号")
        accounts_now = request("/api/accounts?platform=trae")
        check("重复导入不新增账号", len(accounts_now["accounts"]) == 2, str(len(accounts_now["accounts"])))

        try:
            request("/api/accounts/import-storage", "POST", {"content": "{}"})
            check("无凭据的文件被拒绝", False, "竟然通过了")
        except urllib.error.HTTPError:
            check("无凭据的文件被拒绝", True)

        # 14. 统计接口：先清空全部账号，确保不发外部计费请求
        for acc in request("/api/accounts?platform=trae")["accounts"]:
            request("/api/accounts/delete", "POST", {"platform": "trae", "id": acc["id"]})
        credits = request("/api/stats/workbuddy/credits")
        check("GET /api/stats/workbuddy/credits 可用", credits.get("ok") is True and credits.get("accounts") == [])
        wb_usage = request("/api/stats/workbuddy/usage?days=7")
        check("GET /api/stats/workbuddy/usage 可用", wb_usage.get("ok") is True and wb_usage.get("days") == 7)
        trae_usage = request("/api/stats/trae/usage?days=7")
        check("GET /api/stats/trae/usage 可用", trae_usage.get("ok") is True)

        # 15. 目录穿越防护
        try:
            urllib.request.urlopen(BASE + "/../app/server.py", timeout=5)
            check("目录穿越被拦截", False, "竟然读到了")
        except urllib.error.HTTPError as exc:
            check("目录穿越被拦截", exc.code in (404, 400), f"状态码 {exc.code}")
        except Exception:
            check("目录穿越被拦截", True)

        # 16. 访问形态兼容：直连 IP:端口 与 飞牛统一网关/域名（路径带 /app/<appname> 前缀）
        def raw(path: str, method: str = "GET"):
            req = urllib.request.Request(BASE + path, method=method)
            req.add_header("X-QD-Request", "1")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()

        status, headers, _ = raw("/api/status")
        check("直连根路径接口可用", status == 200)

        status, headers, _ = raw("/")
        check("直连根路径返回入口页", status == 200 and "text/html" in headers.get("Content-Type", ""))

        # 网关/探活常用 HEAD：不能返回 501
        status, headers, body = raw("/", "HEAD")
        check("HEAD 请求入口页返回 200", status == 200, f"状态码 {status}")
        check("HEAD 响应带 Content-Length", int(headers.get("Content-Length") or 0) > 0)
        check("HEAD 响应无正文", body == b"", f"正文长度 {len(body)}")
        status, _, _ = raw("/api/status", "HEAD")
        check("HEAD 探活接口返回 200", status == 200, f"状态码 {status}")

        # 网关保留前缀时（飞牛桌面图标 / 域名访问），应用必须能剥掉前缀正常路由
        status, headers, body = raw("/app/qdassistant/api/status")
        check("网关前缀下的接口可路由", status == 200, f"状态码 {status}")
        if status == 200:
            check("网关前缀下接口返回本应用数据", json.loads(body.decode("utf-8")).get("app") == server.APP_NAME)

        status, headers, _ = raw("/app/qdassistant/")
        check("网关前缀下的入口页可用", status == 200 and "text/html" in headers.get("Content-Type", ""))

        # 浏览器会在 <base> 生效前按 /app/ 基准抢先请求资源，需兜底
        status, headers, _ = raw("/app/style.css")
        check("网关前缀下的静态资源兜底", status == 200 and "text/css" in headers.get("Content-Type", ""))

        # 17. 网关套接字入口：飞牛桌面 / 域名 / 中继访问的真实落点。
        #     统一网关把 /app/<应用名>/ 转发到应用目录内的 Unix 套接字，
        #     因此这里必须验证「套接字上跑的就是同一套界面与接口」。
        if not hasattr(socket, "AF_UNIX"):
            check("Unix 套接字入口（平台不支持，跳过）", True)
        else:
            import http.client
            import stat

            sock_path = os.path.join(TMP_DIR, "qdassistant.sock")
            unix_httpd = server.UnixHTTPServer(sock_path, server.Handler)
            unix_thread = threading.Thread(target=unix_httpd.serve_forever, daemon=True)
            unix_thread.start()
            time.sleep(0.3)

            class UnixConn(http.client.HTTPConnection):
                def __init__(self, path, timeout=10):
                    super().__init__("localhost", timeout=timeout)
                    self._path = path

                def connect(self):
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.settimeout(self.timeout)
                    self.sock.connect(self._path)

            def unix_raw(path: str, method: str = "GET"):
                conn = UnixConn(sock_path)
                conn.request(method, path, headers={"X-QD-Request": "1"})
                resp = conn.getresponse()
                body = resp.read()
                status_code = resp.status
                hdrs = dict(resp.getheaders())
                conn.close()
                return status_code, hdrs, body

            try:
                check("套接字文件已建立", os.path.exists(sock_path))
                # 网关以其它身份连接套接字：权限必须对所有人可读写
                mode = stat.S_IMODE(os.stat(sock_path).st_mode)
                check("套接字权限允许网关连接（0666）", mode == 0o666, f"实际 {oct(mode)}")

                status_code, hdrs, body = unix_raw("/")
                check("套接字入口返回入口页", status_code == 200 and "text/html" in hdrs.get("Content-Type", ""),
                      f"状态码 {status_code}")
                check("套接字入口注入版本号", f"__V__".encode() not in body and server.APP_VERSION.encode() in body)

                status_code, hdrs, body = unix_raw("/app.js")
                check("套接字入口提供静态资源", status_code == 200 and "javascript" in hdrs.get("Content-Type", ""))

                status_code, _, body = unix_raw("/api/bootstrap")
                check("套接字入口提供首屏聚合接口", status_code == 200
                      and json.loads(body.decode("utf-8"))["status"]["app"] == server.APP_NAME)

                # 网关是否剥前缀各版本不一致：两种形态都要能路由
                status_code, _, body = unix_raw("/app/qdassistant/api/status")
                check("套接字入口兼容保留前缀", status_code == 200
                      and json.loads(body.decode("utf-8"))["app"] == server.APP_NAME)

                status_code, hdrs, body = unix_raw("/", "HEAD")
                check("套接字入口支持 HEAD 探活", status_code == 200 and body == b"")
            finally:
                unix_httpd.shutdown()
                unix_httpd.server_close()

            # 停止后套接字文件必须消失：残留文件会让下次启动 bind 失败，
            # 也会让网关把一个已死的入口当成活的
            check("停止后套接字文件被清理", not os.path.exists(sock_path))

            # 反向验证：套接字已关闭时连接必须失败，否则上面的用例可能是「没在测套接字」
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(2)
                probe.connect(sock_path)
                probe.close()
                check("套接字关闭后连接失败（反向验证）", False, "竟然连上了")
            except OSError:
                check("套接字关闭后连接失败（反向验证）", True)

            # 重复启动不应被残留文件挡住（bind 前主动清理）
            again = server.UnixHTTPServer(sock_path, server.Handler)
            reopen_ok = os.path.exists(sock_path)
            again.server_close()
            check("套接字可重复建立（残留自愈）", reopen_ok)

    finally:
        httpd.shutdown()
        httpd.server_close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n冒烟测试：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
