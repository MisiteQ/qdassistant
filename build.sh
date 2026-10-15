#!/bin/bash
# ============================================================
# QDAssistant 构建脚本 (Linux / 飞牛 OS)
# 用法: bash build.sh
# 前提: 飞牛 OS 已预置 fnpack（可直接使用），需要 python3
# 产物: qdassistant-<版本>-x86.fpk 与 qdassistant-<版本>-arm.fpk
#
# 说明：纯原生打包 —— 打进去的就是 app/ 下的 Python 源码 + 前端静态文件，
#       安装后由 cmd/main 直接用系统 python3 起进程，不依赖任何容器/镜像。
# ============================================================
set -e
cd "$(dirname "$0")"

# 打包时应排除的开发期产物与辅助目录（打包期间临时移出，结束后移回）
EXCLUDE_DIRS=".demo-data .preview .workbuddy .git docs tests tools"
STAGE=""

cleanup() {
    # 无论成功失败都要把移走的目录放回来，否则源码树会残缺
    if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then
        for d in $EXCLUDE_DIRS; do
            [ -e "$STAGE/$d" ] && mv -f "$STAGE/$d" "./$d" 2>/dev/null || true
        done
        rm -rf "$STAGE" 2>/dev/null || true
    fi
    # 恢复源 manifest
    [ -n "$TMP_MANIFEST" ] && [ -f "$TMP_MANIFEST" ] && cp -f "$TMP_MANIFEST" manifest 2>/dev/null || true
}
trap cleanup EXIT

echo "[1/4] 清理 Python 字节码缓存..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true

echo "[2/4] 移出开发期目录（不进 fpk）..."
STAGE=$(mktemp -d)
for d in $EXCLUDE_DIRS; do
    [ -e "./$d" ] && mv -f "./$d" "$STAGE/$d"
done
echo "      已移出: $EXCLUDE_DIRS"

echo "[3/4] 检查 fnpack..."
if [ -x ./fnpack ]; then
    FNPACK=./fnpack
elif command -v fnpack >/dev/null 2>&1; then
    FNPACK=fnpack
else
    echo "错误：未找到 fnpack 命令。"
    echo "  飞牛 OS 预置了 fnpack，若提示找不到，请确认 PATH 包含 /usr/local/bin"
    exit 1
fi

VERSION=$(sed -n 's/^version=//p' manifest | head -1 | tr -d '[:space:]')
if [ -z "$VERSION" ]; then
    echo "错误：无法从 manifest 解析 version"
    exit 1
fi
echo "[4/4] 打包 x86 / arm（版本 $VERSION）..."

TMP_MANIFEST=$(mktemp)
cp manifest "$TMP_MANIFEST"

for plat in x86 arm; do
    sed "s/^platform=.*/platform=$plat/" "$TMP_MANIFEST" > manifest
    "$FNPACK" build
    mv -f qdassistant.fpk "qdassistant-$VERSION-$plat.fpk"
    echo "  -> qdassistant-$VERSION-$plat.fpk"
done

echo ""
echo "打包完成！"
echo "  qdassistant-$VERSION-x86.fpk （x86 机型）"
echo "  qdassistant-$VERSION-arm.fpk （arm64 机型）"
echo "将对应架构的 .fpk 拷贝到飞牛 OS，在 应用中心 -> 手动安装 中安装。"
