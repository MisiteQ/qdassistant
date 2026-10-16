# QDAssistant · 签到助手

<p>
  <img alt="Version" src="https://img.shields.io/badge/version-1.0.2-blue">
  <img alt="fnOS" src="https://img.shields.io/badge/fnOS-x86%20%7C%20arm64-success">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-orange">
</p>

WorkBuddy 与 Trae **双平台自动签到**应用（FPK 原生应用）：装到飞牛 fnOS 上 7×24 常驻，到点自动完成两平台每日签到 —— **不用再每天打开电脑、打开两个软件手动点**。纯 Python 标准库 + 单文件前端，**零第三方依赖**，凭据只保存在 NAS 本机。

- 当前版本：**v1.0.2**
- 作者：**Misite齊**（<https://github.com/MisiteQ>）
- 适用平台：fnOS **x86 + arm64**（最低系统版本 0.9.0）
- 默认端口：**8790**（可改，Web 面板与 API 共用；桌面 / 域名访问以飞牛统一网关为主入口，此端口保留给局域网直连与端口转发，网关套接字不可用时自动回退）
- 运行身份：**应用自身用户（无需 root）** —— 只读写自己的数据目录，不碰系统硬件、Docker 或其他应用

## ✨ 功能

- **WorkBuddy 签到**：国内版 `daily-checkin`；token 失效自动刷新后重试（带 `refresh_token` 时），惰性刷新避免无谓请求；每账号在签到窗口内错峰到点触发
- **Trae 签到**：先 `status` 预检，已签则跳过不再打扰上游，未签才 `claim` 领取；网络异常自动重试，业务失败进冷却（401 永久 / 429 短冷却 / 5xx 渐进），积分三层兜底
- **两种凭据录入**：双平台均支持 **OAuth 登录** 与 **手动粘贴**（WorkBuddy `access_token` / Trae `JWT`）——NAS 上没有桌面客户端也能完成授权
- **凭据自动续期**：WorkBuddy token 惰性刷新；Trae JWT 过期前用 `refresh_token` 换新，界面显示剩余有效期并提前告警
- **后台 7×24 调度**：可配置每日签到时间段（如 09:00–12:00），窗口内每账号按 (日期, 账号) 确定性抽一个分钟；另有巡检间隔兜底补签与失败恢复
- **Web 管理界面**：总览（今日状态 + 调度 + 积分 / Token 统计）/ 签到 / 设置（账号管理 + 服务端 + 配置导出）/ 关于（版本信息 + 日志）四个页面，手机与桌面自适应
- **凭据安全**：展示一律脱敏（仅首尾各 4 位），日志不落 token / JWT；可设访问口令
- **配置导出 / 导入**：设置页把全部已添加账号与签到策略导出成 JSON，拷到另一台 NAS 直接导入即可，不用逐个重新登录（导出不含监听地址 / 端口 / 口令）

## 📦 安装

### 方式一：FnDepot 应用源（推荐）

在飞牛 fnOS 上安装 [FnDepot](https://github.com/EWEDLCM/FnDepot) 客户端后，添加作者的应用源即可搜索「签到助手」一键安装 / 升级：

```
https://github.com/MisiteQ/FnDepot
```

### 方式二：手动安装 FPK

1. 到 [Releases](https://github.com/MisiteQ/qdassistant/releases) 按 NAS 架构下载：`qdassistant-1.0.2-x86.fpk`（x86 机型）或 `qdassistant-1.0.2-arm.fpk`（arm64 机型）
2. 飞牛 OS → **应用中心** → 左下角 **手动安装** → 选择 fpk 文件
3. 安装后从飞牛桌面点开 **QDAssistant**；也可在同网段直接访问 `http://<NAS_IP>:8790`

> 若「手动安装」入口被关闭，SSH 执行：`appcenter-cli manual-install enable`

### 方式三：直接运行（无需 fpk）

不需要容器、不需要编译，NAS 上有 `python3` 就能直接跑：

```bash
python3 app/server.py                # 默认 0.0.0.0:8790
python3 app/server.py --port 9000 --no-open --data-dir /path/to/data
nohup python3 app/server.py --no-open > qdassistant.log 2>&1 &
```

可选参数：`--host` `--port` `--data-dir` `--no-open`（不自动开浏览器）`--no-scheduler`（只开面板，不跑后台签到）。

## 🔑 添加账号

**WorkBuddy**（设置页 → WorkBuddy）

- **OAuth 登录（推荐）**：点击生成授权链接，在任意设备浏览器完成授权，服务端自动轮询入库；
- 或手动粘贴 `access_token`（建议同时粘贴 `refresh_token` 以便自动续期）；
- 档位选「国内版 cn」——国际版（ai）没有签到接口，会自动跳过。

**Trae**（设置页 → Trae）

- **从 Trae 客户端导入凭据（推荐）**：把本机 Trae 客户端全局存储文件 `storage.json` 的内容整段粘贴进来，服务端用纯标准库 AES-128-CBC 解密出 JWT 并入库（含 `refresh_token` 时支持自动续期）；见下方「Trae 凭据导入」小节；
- **OAuth 登录**：点击后先弹出「授权须知」（回调页打不开是正常现象），打开 Trae 授权页并完成授权 → 浏览器跳到 `127.0.0.1:17388` 的打不开页面 —— **复制地址栏完整 URL** 粘贴回输入框即完成入库；
- 回填框带 **10 分钟倒计时**（AuthCode 有效期）与**粘贴即时校验**（凭证类型 / 是否被截断 / 是否已过期），过期会提示重新发起登录而不是报错；
- 或手动粘贴 JWT（支持 `Cloud-IDE-JWT ` 前缀或裸 token）；
- 同 `user_id` 自动去重（更新而不是重复添加）。

> Trae 的 AuthCode 交换带设备签名校验，完整链路需要本机 Trae 客户端的设备私钥，NAS 上不存在该客户端，因此本服务只实现无签名的协议形态；换取失败时请改用「从 Trae 客户端导入凭据」或手动粘贴 JWT。

### Trae 凭据导入

NAS 上没有 Trae 客户端，OAuth 回调即使正确回填也无法完成设备签名验证。最稳的入库方式是把本机客户端已保存的登录态直接贴进面板：

1. 在**装过 Trae 客户端并已登录**的电脑上找到全局存储文件：
   - Windows：`%APPDATA%\Trae CN\User\globalStorage\storage.json`（新版目录名可能是 `TRAE SOLO CN` 或 `Trae`）
   - macOS：`~/Library/Application Support/Trae CN/User/globalStorage/storage.json`
2. 把文件内容整段复制，粘贴到 QDAssistant 设置页 → **Trae** → **从 Trae 客户端导入凭据**；
3. 点击「导入并添加账号」，服务端会直接在内存中 AES 解密并提取 JWT + refresh_token，然后入库；
4. 解密过程使用纯 Python 标准库实现，**零第三方依赖**；内容仅在服务端内存中解析，不落盘、不进日志。

## 🛠 从源码打包

需要 [fnpack](https://developer.fnnas.com/docs/cli/fnpack/) 命令行工具：**Windows 下可直接把 `fnpack.exe` 放到 `tools/` 或项目根目录**，打包脚本会优先使用（脚本也会回退查找 PATH 中的 `fnpack`）。

构建脚本会自动读取 `manifest` 中的版本号，**一次构建同时产出带版本号的双架构安装包**：

- `qdassistant-<版本>-x86.fpk`（x86 机型）
- `qdassistant-<版本>-arm.fpk`（arm64 机型）

### Windows（PowerShell 5+）

```powershell
# 需 Python 3
.\build.ps1
```

### Linux / fnOS

```bash
bash build.sh
```

> 打包过程中脚本会临时切换 `manifest` 的 `platform` 字段分别构建，结束后自动恢复原值；发布新版本时只需修改 `manifest` 的 `version` 与 `changelog` 后重新执行脚本。

## 📁 项目结构

```
manifest                飞牛应用清单（版本、显示名、端口、权限、更新日志）
cmd/                    飞牛生命周期脚本（安装回调、启动、卸载清理、升级等）
config/                 飞牛权限与资源声明
app/server.py           后端：HTTP API + 静态资源 + 调度启动（零依赖）
app/core/               核心层：util / store / variant / wb_accounts / wb_checkin /
                        trae_identity / trae_checkin / trae_storage / oauth / scheduler /
                        wb_stats / trae_stats
app/www/                前端：零依赖原生 HTML/JS/CSS（无构建步骤）
app/ui/                 fnOS 桌面图标与入口配置
tools/fnpack.exe        打包工具（Windows，可选随项目携带）
tools/make_icon.py      应用图标生成
tools/seed_demo.py      演示数据生成（本地预览界面用）
tests/                  单元测试 + 冒烟测试
build.ps1 / build.sh    Windows / Linux 双架构打包脚本
```

## 🔒 隐私与安全

- 数据目录默认 `~/.qdassistant`（fpk 安装时为应用 var 目录），可用 `--data-dir` 或环境变量 `QD_DATA_DIR` 覆盖；账号 / 配置 / 日志全部为 JSON 文件，便于备份迁移
- 凭据脱敏展示（仅首尾各 4 位），日志不落 token / JWT
- 状态变更接口需携带 `X-QD-Request: 1` 请求头（CSRF 闸，浏览器跨站请求发不出）
- **建议开启访问口令**（设置页），尤其 NAS 对局域网或公网开放时
- **忘记口令**：SSH 登录 NAS，编辑数据目录下的 `config.json`，删除 `access_password` 字段后重启应用
- 纯 Python 标准库实现，零第三方依赖；除签到接口与 OAuth 授权外不与任何外部服务通信

## 🧪 测试

```bash
python3 tests/test_unit.py    # 纯函数单测
python3 tests/test_smoke.py   # 服务端冒烟测试（真实起 HTTP 服务走全接口）
```

## 🙏 致谢

- 飞牛 fnOS 与 [FnDepot](https://github.com/EWEDLCM/FnDepot)

## 📋 版本历史

| 版本    | 内容                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| v1.0.2 | **域名访问修复**：桌面入口改走飞牛统一网关（`/app/qdassistant/` + 应用内置 Unix 套接字），页面地址与飞牛桌面同源 —— 此前域名访问时桌面会把内容指向应用端口，而该端口未在域名侧放通，表现为局域网 / 中继能打开、域名打开却是加载失败；**入口双通道**：TCP 端口保留给局域网直连与端口转发，与网关入口共用同一套路由，套接字不可用时自动退回端口；**套接字残留清理**：启动 / 停止 / 安装 / 升级 / 卸载各环节统一清理 |
| v1.0.1 | **飞牛桌面 / 域名访问修复**：首屏改为单请求聚合（`GET /api/bootstrap`），页面数据请求全部串行化 —— 统一网关对并发转发不可靠，首屏并发接口会一起悬住，表现为从飞牛桌面点开应用后一直转圈、提示「响应时间过长」；**升级数据保护**：覆盖安装若触发卸载钩子，10 分钟窗口内不再删除已添加的账号与配置；版本号统一升到 1.0.1 |
| v1.0.0 | 首个正式版本：WorkBuddy 与 Trae **双平台自动签到**（status 预检 / 错误分类冷却 / token 与 JWT 自动续期）；**多种凭据录入**：OAuth 登录、Trae 客户端凭据导入、手动粘贴；**积分 / Token 统计**：两平台余额、积分包与到期、请求用量与会话 Token 构成；**一键签到**双平台并行；页面布局 总览 / 签到 / 设置 / 关于；**配置导出 / 导入**换机免重新登录 |


## 📄 许可

[MIT](LICENSE) © 2026 Misite齊（<https://github.com/MisiteQ>）
