#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 Trae 桌面客户端的本地登录凭据，免 OAuth 直接入库。

**为什么需要它**：Trae 的 OAuth 授权把凭证推给浏览器所在机器的客户端（回调
`127.0.0.1:17388`），且 AuthCode 交换要校验设备私钥签名——NAS 上没有该客户端，
这条链路必然走不通。但客户端登录成功后会把凭据写在**本机**的全局存储里，
用户把那个文件的内容贴进面板，本模块即可解出 JWT 与 refresh_token 直接入库。

**凭据格式**（客户端全局存储 `storage.json`，键 `iCubeAuthInfo://icube.cloudide`）：

    base64( header(6) | random(32) | AES-128-CBC( SHA512(明文) | 明文 ) )

- header 有两套：`tc\\x05\\x10\\x00\\x00`（通用）与 `\\x12\\x39\\x20\\x20\\x02\\x03`（私有），
  各对应一组盐值；
- 密钥派生：salt = SALT_a ^ SALT_b，final = SHA512( SHA512(random) | salt )，
  前 16 字节为 AES 密钥、其后 16 字节为 IV；
- 解密后前 64 字节是明文的 SHA512，用于校验；剩余为明文 JSON
  （含 `token` / `userId` / `refreshToken` / `expiredAt` / `host`）。

本项目**只读取、绝不回写**客户端文件；明文 token 不进任何日志。
AES 用纯标准库实现（不允许引入第三方依赖）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json

AUTH_KEY = "iCubeAuthInfo://icube.cloudide"

HEADER_SIZE = 6
RANDOM_BYTES_LEN = 32
HASH_SIZE = 64
AES_KEY_SIZE = 16
IV_SIZE = 16
BLOCK_SIZE = 16

# 凭据头部：前一种为通用加密，后一种为私有加密（盐值不同）
HEADER_AES = bytes((0x74, 0x63, 0x05, 0x10, 0x00, 0x00))
HEADER_AES_PRIVATE = bytes((0x12, 0x39, 0x20, 0x20, 0x02, 0x03))

# 密钥派生盐值（客户端内置常量，两组分别对应上面两种头部）
_SALT_A = bytes(
    (
        82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
        124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
        84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
        8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37,
    )
)
_SALT_B = bytes(
    (
        31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
        96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
        160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
        23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125,
    )
)
_SALT_C = bytes(
    (
        191, 192, 216, 250, 122, 246, 220, 97, 31, 254, 98, 27, 8, 72, 71, 176,
        135, 99, 96, 18, 127, 101, 203, 104, 211, 102, 191, 125, 37, 72, 150, 156,
        51, 229, 121, 35, 17, 153, 141, 177, 110, 131, 150, 128, 172, 255, 254, 6,
        18, 140, 55, 62, 236, 249, 135, 64, 135, 12, 117, 4, 89, 149, 168, 209,
    )
)
_SALT_D = bytes(
    (
        246, 204, 26, 232, 232, 70, 129, 109, 223, 146, 169, 242, 23, 241, 105, 145,
        50, 196, 165, 42, 254, 120, 3, 54, 244, 207, 209, 85, 53, 6, 138, 106, 175,
        148, 31, 204, 186, 186, 165, 182, 87, 142, 49, 10, 39, 110, 26, 154, 86, 56,
        173, 125, 18, 64, 198, 225, 99, 99, 83, 82, 191, 134, 76, 170,
    )
)


# ---------------------------------------------------------------------------
# AES-128（纯标准库实现）
# ---------------------------------------------------------------------------


def _build_tables() -> tuple[list[int], list[int]]:
    """生成 AES S 盒与逆 S 盒（GF(2^8) 求逆 + 仿射变换），避免硬编码出错。"""
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x ^= ((x << 1) ^ 0x11B) & 0xFF if x & 0x80 else (x << 1)
        x &= 0xFF
    for i in range(255, 512):
        exp[i] = exp[i - 255]

    sbox = [0] * 256
    sbox[0] = 0x63
    for i in range(1, 256):
        inv = exp[255 - log[i]]
        value = inv
        for _ in range(4):
            inv = ((inv << 1) | (inv >> 7)) & 0xFF
            value ^= inv
        sbox[i] = value ^ 0x63

    inv_sbox = [0] * 256
    for i, value in enumerate(sbox):
        inv_sbox[value] = i
    return sbox, inv_sbox


_SBOX, _INV_SBOX = _build_tables()


def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法（AES 域多项式 0x11B）。"""
    product = 0
    for _ in range(8):
        if b & 1:
            product ^= a
        carry = a & 0x80
        a = (a << 1) & 0xFF
        if carry:
            a ^= 0x1B
        b >>= 1
    return product


def _expand_key(key: bytes) -> list[list[int]]:
    """AES-128 密钥扩展：11 个 16 字节轮密钥（44 个 4 字节字）。"""
    words = [list(key[i * 4 : (i + 1) * 4]) for i in range(4)]
    rcon = 1
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[byte] for byte in temp]
            temp[0] ^= rcon
            rcon = ((rcon << 1) ^ 0x1B) & 0xFF if rcon & 0x80 else (rcon << 1) & 0xFF
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    return words


def _decrypt_block(block: bytes, words: list[list[int]]) -> bytes:
    """解密单个 16 字节分组。状态按列填充：state[r + 4c] = 第 r 行第 c 列。"""
    state = list(block)

    def add_round_key(round_index: int) -> None:
        for c in range(4):
            for r in range(4):
                state[r + 4 * c] ^= words[round_index * 4 + c][r]

    def inv_shift_rows() -> None:
        # 第 1/2/3 行分别循环右移 1/2/3 字节
        for r in range(1, 4):
            row = [state[r + 4 * c] for c in range(4)]
            row = row[-r:] + row[:-r]
            for c in range(4):
                state[r + 4 * c] = row[c]

    def inv_sub_bytes() -> None:
        for i in range(16):
            state[i] = _INV_SBOX[state[i]]

    def inv_mix_columns() -> None:
        for c in range(4):
            col = [state[4 * c], state[4 * c + 1], state[4 * c + 2], state[4 * c + 3]]
            state[4 * c + 0] = (
                _gmul(col[0], 0x0E) ^ _gmul(col[1], 0x0B) ^ _gmul(col[2], 0x0D) ^ _gmul(col[3], 0x09)
            )
            state[4 * c + 1] = (
                _gmul(col[0], 0x09) ^ _gmul(col[1], 0x0E) ^ _gmul(col[2], 0x0B) ^ _gmul(col[3], 0x0D)
            )
            state[4 * c + 2] = (
                _gmul(col[0], 0x0D) ^ _gmul(col[1], 0x09) ^ _gmul(col[2], 0x0E) ^ _gmul(col[3], 0x0B)
            )
            state[4 * c + 3] = (
                _gmul(col[0], 0x0B) ^ _gmul(col[1], 0x0D) ^ _gmul(col[2], 0x09) ^ _gmul(col[3], 0x0E)
            )

    add_round_key(10)
    for round_index in range(9, 0, -1):
        inv_shift_rows()
        inv_sub_bytes()
        add_round_key(round_index)
        inv_mix_columns()
    inv_shift_rows()
    inv_sub_bytes()
    add_round_key(0)
    return bytes(state)


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC 解密并去除 PKCS#7 填充。"""
    if len(data) % BLOCK_SIZE:
        raise ValueError("密文长度不是分组长度的整数倍")
    if not data:
        raise ValueError("密文为空")

    words = _expand_key(key)
    out = bytearray()
    previous = iv
    for offset in range(0, len(data), BLOCK_SIZE):
        block = data[offset : offset + BLOCK_SIZE]
        plain = _decrypt_block(block, words)
        out.extend(bytes(plain[i] ^ previous[i] for i in range(BLOCK_SIZE)))
        previous = block

    pad = out[-1]
    if not 1 <= pad <= BLOCK_SIZE or len(out) < pad:
        raise ValueError("填充字节不合法")
    if any(byte != pad for byte in out[-pad:]):
        raise ValueError("填充校验失败（密钥或数据不匹配）")
    return bytes(out[:-pad])


# ---------------------------------------------------------------------------
# 凭据解析
# ---------------------------------------------------------------------------


def _xor_salt(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _derive_key_iv(random_bytes: bytes, private: bool) -> tuple[bytes, bytes]:
    """密钥派生：SHA512( SHA512(random) | salt ) → key(16) + iv(16)。"""
    salt = _xor_salt(_SALT_C, _SALT_D) if private else _xor_salt(_SALT_A, _SALT_B)
    digest = hashlib.sha512(hashlib.sha512(random_bytes).digest() + salt).digest()
    return digest[:AES_KEY_SIZE], digest[AES_KEY_SIZE : AES_KEY_SIZE + IV_SIZE]


def _detect_header(header: bytes) -> str:
    if header == HEADER_AES:
        return "aes"
    if header == HEADER_AES_PRIVATE:
        return "aes-private"
    return "unknown"


def decrypt_tc_value(value: str) -> str:
    """解密一条客户端凭据值（base64 文本）→ 明文 JSON 字符串。"""
    try:
        raw = base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"凭据不是合法 base64：{exc}") from exc

    minimum = HEADER_SIZE + RANDOM_BYTES_LEN + HASH_SIZE + BLOCK_SIZE
    if len(raw) < minimum:
        raise ValueError("凭据长度不足，可能不是完整的客户端凭据")

    enc_type = _detect_header(raw[:HEADER_SIZE])
    if enc_type == "unknown":
        raise ValueError(f"无法识别的凭据头部：{raw[:HEADER_SIZE].hex()}")

    random_bytes = raw[HEADER_SIZE : HEADER_SIZE + RANDOM_BYTES_LEN]
    cipher = raw[HEADER_SIZE + RANDOM_BYTES_LEN :]
    key, iv = _derive_key_iv(random_bytes, enc_type == "aes-private")

    try:
        decrypted = _aes_cbc_decrypt(key, iv, cipher)
    except ValueError as exc:
        raise ValueError(f"AES 解密失败：{exc}") from exc

    if len(decrypted) <= HASH_SIZE:
        raise ValueError("解密结果缺少校验段")

    stored_hash = decrypted[:HASH_SIZE]
    plaintext = decrypted[HASH_SIZE:]
    if hashlib.sha512(plaintext).digest() != stored_hash:
        raise ValueError("校验失败：凭据可能已损坏，或来自不同版本的客户端")
    return plaintext.decode("utf-8", errors="replace")


def extract_auth(storage: dict) -> dict:
    """从客户端全局存储里取出登录凭据（明文或加密形态都支持）。"""
    raw = storage.get(AUTH_KEY)
    if raw is None:
        # 键名大小写/空格差异兜底
        for key, value in storage.items():
            if isinstance(key, str) and key.strip().lower() == AUTH_KEY.lower():
                raw = value
                break
    if raw is None:
        raise ValueError(f"文件里没有 {AUTH_KEY} 键——请确认复制的是客户端全局存储文件，且客户端已登录")

    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("凭据内容为空——请确认 Trae 客户端已登录过一次")

    text = raw.strip()
    if text.startswith("{") or text.startswith('"'):
        # 少数版本直接存明文 JSON
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}

    parsed = json.loads(decrypt_tc_value(text))
    if not isinstance(parsed, dict):
        raise ValueError("凭据解出的内容不是对象")
    return parsed


def parse_storage(content: str) -> dict:
    """解析用户粘贴的客户端全局存储文件内容。"""
    text = (content or "").strip()
    if not text:
        raise ValueError("请先粘贴客户端全局存储文件的内容")
    try:
        storage = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"内容不是合法 JSON：{exc}") from exc
    if not isinstance(storage, dict):
        raise ValueError("内容不是一个 JSON 对象")
    return storage


def credentials_from_storage(content: str) -> dict:
    """从粘贴内容里解出可入库的 Trae 凭据。

    返回 `{"jwt", "refreshToken", "userId", "host", "expiredAt", "userName", "email"}`；
    `jwt` 统一带 `Cloud-IDE-JWT ` 前缀，与手动粘贴保持一致。
    `userName` / `email` 是凭据里自带的身份线索（有则带出，用于多账号区分）。
    """
    auth = extract_auth(parse_storage(content))

    token = auth.get("token") or auth.get("accessToken") or auth.get("access_token") or ""
    token = token.strip() if isinstance(token, str) else ""
    if not token:
        raise ValueError("凭据里没有 token 字段——请确认客户端已登录且凭据未过期")

    def _first_str(*values) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    jwt = token if token.startswith("Cloud-IDE-JWT ") else f"Cloud-IDE-JWT {token}"
    refresh = auth.get("refreshToken") or auth.get("refresh_token") or ""
    return {
        "jwt": jwt,
        "refreshToken": refresh.strip() if isinstance(refresh, str) else "",
        "userId": auth.get("userId") or auth.get("user_id") or "",
        "host": auth.get("host") or "",
        "expiredAt": auth.get("expiredAt") or auth.get("expired_at"),
        "userName": _first_str(
            auth.get("userName"),
            auth.get("NickName"),
            auth.get("nickName"),
            auth.get("nickname"),
            auth.get("ScreenName"),
            auth.get("screenName"),
            auth.get("name"),
        ),
        "email": _first_str(auth.get("email"), auth.get("Email"), auth.get("mail")),
    }
