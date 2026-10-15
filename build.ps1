# QDAssistant 构建脚本（Windows）
# 产出: qdassistant-<版本>-x86.fpk 与 qdassistant-<版本>-arm.fpk
#
# fnpack 查找顺序：
#   1. tools\fnpack.exe     （随项目携带，推荐）
#   2. 项目根目录 fnpack.exe
#   3. 相邻项目 /d/CODE/fnpack/fnpack.exe
#   4. PATH 中的 fnpack
#
# 纯原生打包：进包的就是 app/ 下的 Python 源码 + 前端静态文件，
# 安装后由 cmd/main 直接用系统 python3 起进程，不依赖任何容器 / 镜像。

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ---- 解析 Python ----
# WindowsApps 里有个 python.exe 商店占位 stub，排在前面的 PATH 会让 `python`
# 静默失败（exit 9009）且无任何输出。优先用 py 启动器，其次找真实的 python.exe。
$PYEXE = $null
$PYARG = $null
$pyCmd = Get-Command py -ErrorAction SilentlyContinue
if ($pyCmd) {
    & py -3 --version *> $null
    if ($LASTEXITCODE -eq 0) {
        $PYEXE = $pyCmd.Source
        $PYARG = "-3"
    }
}
if (-not $PYEXE) {
    foreach ($cand in @(
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
            "C:\Program Files\Python312\python.exe",
            "C:\Program Files\Python311\python.exe"
        )) {
        if (Test-Path $cand) { $PYEXE = $cand; break }
    }
}
if (-not $PYEXE) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { $PYEXE = $cmd.Source }
}
if (-not $PYEXE) {
    Write-Host "错误：未找到可用的 Python 3（py 启动器或 python.exe 均不可用）。" -ForegroundColor Red
    exit 1
}
Write-Host "使用 Python: $PYEXE $($PYARG)"

function Invoke-Py {
    if ($script:PYARG) { & $script:PYEXE $script:PYARG @args }
    else { & $script:PYEXE @args }
}

Write-Host "[1/4] 生成应用图标..."
Invoke-Py tools\make_icon.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/4] 清理 Python 字节码缓存..."
Get-ChildItem -Path . -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path . -Recurse -File -Filter "*.pyc" -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue

Write-Host "[3/4] 运行单元测试 + 冒烟测试..."
Invoke-Py tests\test_unit.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Invoke-Py tests\test_smoke.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# ---- 定位 fnpack ----
$FNPACK = $null
foreach ($candidate in @(
        (Join-Path $PSScriptRoot "tools\fnpack.exe"),
        (Join-Path $PSScriptRoot "fnpack.exe"),
        "D:\CODE\fnpack\fnpack.exe"
    )) {
    if (Test-Path $candidate) { $FNPACK = $candidate; break }
}
if (-not $FNPACK) {
    $cmd = Get-Command fnpack -ErrorAction SilentlyContinue
    if ($cmd) { $FNPACK = $cmd.Source }
}
if (-not $FNPACK) {
    Write-Host "错误：未找到 fnpack。" -ForegroundColor Red
    Write-Host "请从飞牛开发者文档下载 fnpack，放到 tools\fnpack.exe 后重试。"
    exit 1
}
Write-Host "使用 fnpack: $FNPACK"

# ---- 读取版本号 ----
$VERSION = (Select-String -Path "manifest" -Pattern "^version=(.*)$").Matches[0].Groups[1].Value.Trim()
if (-not $VERSION) {
    Write-Host "错误：无法从 manifest 解析 version" -ForegroundColor Red
    exit 1
}

Write-Host "[4/4] 打包 x86 / arm（版本 $VERSION）..."

Copy-Item "manifest" "manifest.bak" -Force
try {
    # 必须用 .NET 读写：PowerShell 5.1 的 Get-Content 默认按系统 ANSI(GBK) 解码
    # UTF-8 文件，再以 UTF8 写回会形成双重编码，manifest 里的中文（显示名 /
    # 描述 / 更新日志）进包后全是乱码。
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $manifestPath = Join-Path $PSScriptRoot "manifest"
    $bakPath = Join-Path $PSScriptRoot "manifest.bak"
    $source = [System.IO.File]::ReadAllText($bakPath, $utf8)

    foreach ($plat in @("x86", "arm")) {
        [System.IO.File]::WriteAllText(
            $manifestPath,
            ($source -replace "(?m)^platform=.*", "platform=$plat"),
            $utf8
        )
        & $FNPACK build
        if ($LASTEXITCODE -ne 0) { throw "fnpack build 失败（platform=$plat）" }
        if (Test-Path "qdassistant.fpk") {
            Move-Item "qdassistant.fpk" "qdassistant-$VERSION-$plat.fpk" -Force
            Write-Host "  -> qdassistant-$VERSION-$plat.fpk" -ForegroundColor Green
        }
    }
}
finally {
    Move-Item "manifest.bak" "manifest" -Force
}

Write-Host ""
Write-Host "打包完成！" -ForegroundColor Green
Write-Host "  qdassistant-$VERSION-x86.fpk （x86 机型）"
Write-Host "  qdassistant-$VERSION-arm.fpk （arm64 机型）"
Write-Host "将对应架构的 .fpk 拷贝到飞牛 OS，在 应用中心 -> 手动安装 中安装。"
