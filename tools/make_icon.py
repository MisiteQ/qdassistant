#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 QDAssistant 应用图标（纯标准库：zlib + struct，无第三方依赖）。

图标源：项目根目录的 `QDAssistant.png`（作者提供的 logo）。
若该文件不存在，则回退到内置的程序化绘制（蓝色渐变圆角方块 + 白色对勾）。

产物：
    ICON_256.PNG                  256×256  fpk 包体根级图标（应用中心读取，必须）
    ICON.PNG                       64×64   fpk 包体根级图标（必须）
    app/ui/ICON_256.PNG           256×256  应用目录图标
    app/ui/ICON.PNG                64×64   应用目录图标
    app/ui/images/icon-256.png    256×256  桌面启动图标
    app/ui/images/icon-64.png      64×64   桌面启动图标
    app/www/images/icon-64.png     64×64   Web 侧边栏品牌 logo
    app/www/favicon.ico        64/32/16    浏览器标签页图标

用法：
    python3 tools/make_icon.py
"""

from __future__ import annotations

import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UI_DIR = os.path.join(ROOT, "app", "ui")
WWW_DIR = os.path.join(ROOT, "app", "www")
SOURCE_LOGO = os.path.join(ROOT, "QDAssistant.png")


# ---------------------------------------------------------------------------
# PNG 解码（8 位深，非交错；支持 RGBA / RGB / 灰度 / 灰度+α / 调色板）
# ---------------------------------------------------------------------------


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def png_decode(data: bytes):
    """解码 PNG 为 `(width, height, rows)`，rows 为 RGBA 元组二维数组。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是合法的 PNG 文件")

    width = height = bitdepth = colortype = 0
    idat = bytearray()
    palette = None
    trns = None
    offset = 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        ctype = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        if ctype == b"IHDR":
            width, height, bitdepth, colortype, _c, _f, interlace = struct.unpack(
                ">IIBBBBB", body
            )
            if interlace:
                raise ValueError("暂不支持交错（interlaced）PNG")
            if bitdepth != 8:
                raise ValueError(f"暂不支持位深 {bitdepth}（仅支持 8）")
        elif ctype == b"PLTE":
            palette = body
        elif ctype == b"tRNS":
            trns = body
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
        offset += 12 + length

    raw = zlib.decompress(bytes(idat))
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colortype]
    stride = width * channels

    # 逐行反 filter，还原真实扫描线
    flat = bytearray()
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(a, b, c)) & 0xFF
        else:
            raise ValueError(f"未知 filter 类型 {ftype}")
        flat += line
        prev = line

    # 统一转成 RGBA
    rows = []
    for y in range(height):
        base = y * stride
        row = []
        for x in range(width):
            i = base + x * channels
            if colortype == 6:
                row.append((flat[i], flat[i + 1], flat[i + 2], flat[i + 3]))
            elif colortype == 2:
                row.append((flat[i], flat[i + 1], flat[i + 2], 255))
            elif colortype == 0:
                row.append((flat[i], flat[i], flat[i], 255))
            elif colortype == 4:
                row.append((flat[i], flat[i], flat[i], flat[i + 1]))
            else:  # 3 调色板
                idx = flat[i]
                r, g, b = palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]
                a = trns[idx] if (trns and idx < len(trns)) else 255
                row.append((r, g, b, a))
        rows.append(row)
    return width, height, rows


# ---------------------------------------------------------------------------
# 缩放：Lanczos3 + alpha 预乘（避免透明边缘出现黑边）
# ---------------------------------------------------------------------------


def _lanczos(t: float, radius: int = 3) -> float:
    if t == 0:
        return 1.0
    if abs(t) >= radius:
        return 0.0
    x = math.pi * t
    return radius * math.sin(x) * math.sin(x / radius) / (x * x)


def _weights(center: float, limit: int, radius: int):
    start = max(0, int(center) - radius + 1)
    end = min(limit - 1, int(center) + radius)
    items = []
    for i in range(start, end + 1):
        w = _lanczos(center - i, radius)
        if w:
            items.append((i, w))
    return items


def resize(rows, width: int, height: int, target: int):
    """把 RGBA 图像缩放到 target×target。"""
    if width == target and height == target:
        return rows

    # alpha 预乘：透明像素的 RGB 不参与插值，杜绝黑色描边
    premul = [
        [(r * a / 255.0, g * a / 255.0, b * a / 255.0, float(a)) for (r, g, b, a) in row]
        for row in rows
    ]

    scale = target / width
    radius = 3
    # 各输出像素共用同一组纵向权重，先算好避免内层重复计算
    y_weights = [((ty + 0.5) / scale - 0.5, _weights((ty + 0.5) / scale - 0.5, height, radius)) for ty in range(target)]

    out = []
    for _ty, (sy, wys) in enumerate(y_weights):
        row_out = []
        for tx in range(target):
            sx = (tx + 0.5) / scale - 0.5
            wxs = _weights(sx, width, radius)
            acc = [0.0, 0.0, 0.0, 0.0]
            wsum = 0.0
            for y, wy in wys:
                src_row = premul[y]
                for x, wx in wxs:
                    w = wx * wy
                    px = src_row[x]
                    acc[0] += px[0] * w
                    acc[1] += px[1] * w
                    acc[2] += px[2] * w
                    acc[3] += px[3] * w
                    wsum += w
            if wsum <= 0:
                row_out.append((0, 0, 0, 0))
                continue
            pa = acc[3] / wsum
            if pa <= 0.5:
                row_out.append((0, 0, 0, 0))
                continue
            # 反预乘
            inv = 255.0 / pa
            row_out.append(
                (
                    min(255, max(0, int(round(acc[0] / wsum * inv)))),
                    min(255, max(0, int(round(acc[1] / wsum * inv)))),
                    min(255, max(0, int(round(acc[2] / wsum * inv)))),
                    min(255, max(0, int(round(pa)))),
                )
            )
        out.append(row_out)
    return out


# ---------------------------------------------------------------------------
# 编码输出
# ---------------------------------------------------------------------------


def chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def rows_to_png(rows) -> bytes:
    height = len(rows)
    width = len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter: None
        for (r, g, b, a) in row:
            raw += bytes((r, g, b, a))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    return png


def write_png(path: str, rows) -> None:
    png = rows_to_png(rows)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(png)
    height = len(rows)
    width = len(rows[0])
    print(f"  -> {os.path.relpath(path, ROOT)} ({width}x{height}, {len(png)} bytes)")


def write_ico(path: str, sizes) -> None:
    """把多尺寸 PNG 打包成一个 .ico（Vista+ 支持内嵌 PNG）。"""
    blobs = [(size, rows_to_png(_CACHE[size])) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries = b""
    body = b""
    for size, png in blobs:
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0,
            0,
            1,
            32,
            len(png),
            offset,
        )
        body += png
        offset += len(png)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(header + entries + body)
    print(
        f"  -> {os.path.relpath(path, ROOT)} "
        f"({'/'.join(str(s) for s in sizes)}, {6 + 16 * len(blobs) + len(body)} bytes)"
    )


# ---------------------------------------------------------------------------
# 回退：logo 源文件缺失时的程序化绘制
# ---------------------------------------------------------------------------

SIZE = 256
RADIUS = 58
TOP = (47, 111, 235)
BOTTOM = (24, 68, 168)


def lerp(a, b, t):
    return int(a + (b - a) * t)


def in_rounded_rect(x: int, y: int) -> bool:
    cx = min(max(x, RADIUS), SIZE - RADIUS)
    cy = min(max(y, RADIUS), SIZE - RADIUS)
    return (x - cx) ** 2 + (y - cy) ** 2 <= (RADIUS + 0.5) ** 2


def coverage(x: float, y: float) -> float:
    total = 0
    for ox in (0.25, 0.75):
        for oy in (0.25, 0.75):
            if in_rounded_rect(int(x + ox), int(y + oy)):
                total += 0.25
    return total


def point_to_segment(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    qx, qy = ax + t * dx, ay + t * dy
    return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5


def check_alpha(px: float, py: float, half_width: float) -> float:
    a = (72, 136)
    b = (116, 178)
    c = (190, 84)
    total = 0.0
    for ox in (0.25, 0.75):
        for oy in (0.25, 0.75):
            sx, sy = px + ox, py + oy
            d = min(
                point_to_segment(sx, sy, *a, *b),
                point_to_segment(sx, sy, *b, *c),
            )
            if d <= half_width - 0.5:
                total += 0.25
            elif d < half_width + 0.5:
                total += 0.25 * (1.0 - (d - (half_width - 0.5)))
    return min(1.0, total)


def render(size: int = SIZE):
    """蓝色渐变圆角方块 + 白色对勾（签到语义）。"""
    scale = size / SIZE
    half_width = 17 * scale
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            nx, ny = (x + 0.5) / scale, (y + 0.5) / scale
            bg_cov = coverage(nx, ny)
            if bg_cov <= 0:
                row.append((0, 0, 0, 0))
                continue
            t = ny / SIZE
            base = (
                lerp(TOP[0], BOTTOM[0], t),
                lerp(TOP[1], BOTTOM[1], t),
                lerp(TOP[2], BOTTOM[2], t),
            )
            fg = check_alpha(nx, ny, half_width)
            if fg >= 1.0:
                row.append((255, 255, 255, int(255 * bg_cov)))
            elif fg > 0:
                blended = tuple(int(255 * fg + base[i] * (1 - fg)) for i in range(3))
                row.append((*blended, int(255 * bg_cov)))
            else:
                row.append((*base, int(255 * bg_cov)))
        rows.append(row)
    return rows


_CACHE: dict = {}


def main() -> int:
    if os.path.exists(SOURCE_LOGO):
        print(f"使用 logo 源文件: {os.path.basename(SOURCE_LOGO)}")
        with open(SOURCE_LOGO, "rb") as handle:
            width, height, rows = png_decode(handle.read())
        print(f"  源尺寸: {width}x{height}")
        if width != height:
            side = min(width, height)
            print(f"  非正方形，按中心裁剪为 {side}x{side}")
            left = (width - side) // 2
            top = (height - side) // 2
            rows = [row[left : left + side] for row in rows[top : top + side]]
            width = height = side
        mode = "logo"
    else:
        print(f"未找到 {os.path.basename(SOURCE_LOGO)}，回退到程序化绘制图标")
        width = height = SIZE
        rows = render(SIZE)
        mode = "fallback"

    print("生成图标…")
    for target in (256, 128, 64, 32, 16):
        _CACHE[target] = resize(rows, width, height, target)

    # fpk 包体根级图标：应用中心与包管理读取的是根目录这一份，
    # 缺了会导致应用中心里显示空白图标（app/ui 那份只供桌面入口用）。
    write_png(os.path.join(ROOT, "ICON_256.PNG"), _CACHE[256])
    write_png(os.path.join(ROOT, "ICON.PNG"), _CACHE[64])

    write_png(os.path.join(UI_DIR, "ICON_256.PNG"), _CACHE[256])
    write_png(os.path.join(UI_DIR, "ICON.PNG"), _CACHE[64])
    write_png(os.path.join(UI_DIR, "images", "icon-256.png"), _CACHE[256])
    write_png(os.path.join(UI_DIR, "images", "icon-64.png"), _CACHE[64])
    write_png(os.path.join(WWW_DIR, "images", "icon-64.png"), _CACHE[64])
    write_ico(os.path.join(WWW_DIR, "favicon.ico"), (64, 32, 16))
    print(f"完成（模式：{mode}）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
