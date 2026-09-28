"""ISAAC-64 stream used by encrypted WeChat Moments CDN images.

The generator follows Bob Jenkins' public-domain ISAAC-64 algorithm.
"""
from __future__ import annotations

MASK = (1 << 64) - 1


def _mix(words):
    a, b, c, d, e, f, g, h = words
    a = (a - e) & MASK; f ^= h >> 9; h = (h + a) & MASK
    b = (b - f) & MASK; g ^= (a << 9) & MASK; a = (a + b) & MASK
    c = (c - g) & MASK; h ^= b >> 23; b = (b + c) & MASK
    d = (d - h) & MASK; a ^= (c << 15) & MASK; c = (c + d) & MASK
    e = (e - a) & MASK; b ^= d >> 14; d = (d + e) & MASK
    f = (f - b) & MASK; c ^= (e << 20) & MASK; e = (e + f) & MASK
    g = (g - c) & MASK; d ^= f >> 17; f = (f + g) & MASK
    h = (h - d) & MASK; e ^= (g << 14) & MASK; g = (g + h) & MASK
    return [x & MASK for x in (a, b, c, d, e, f, g, h)]


def keystream(seed: int, length: int) -> bytes:
    """Return the exact-length stream in reverse word order, big endian."""
    if not 0 <= seed <= MASK or length < 0:
        raise ValueError('朋友圈媒体密钥或长度无效')
    memory = [0] * 256
    results = [seed] + [0] * 255
    state = [0x9e3779b97f4a7c13] * 8
    for _ in range(4):
        state = _mix(state)
    for source in (results, memory):
        for pos in range(0, 256, 8):
            state = _mix([(v + source[pos + i]) & MASK for i, v in enumerate(state)])
            memory[pos:pos + 8] = state
    a = b = c = 0
    raw = bytearray()
    while len(raw) < length:
        c = (c + 1) & MASK
        b = (b + c) & MASK
        for pos in range(256):
            x = memory[pos]
            direction = pos & 3
            if direction == 0:
                a = ~(a ^ (a << 21)) & MASK
            elif direction == 1:
                a = (a ^ (a >> 5)) & MASK
            elif direction == 2:
                a = (a ^ (a << 12)) & MASK
            else:
                a = (a ^ (a >> 33)) & MASK
            a = (a + memory[(pos + 128) & 255]) & MASK
            y = (memory[(x >> 3) & 255] + a + b) & MASK
            memory[pos] = y
            b = (memory[(y >> 11) & 255] + x) & MASK
            results[pos] = b
        for word in reversed(results):
            raw.extend(word.to_bytes(8, 'big'))
    return bytes(raw[:length])


def decrypt_image(data: bytes, seed: str) -> bytes:
    if not seed or not seed.isascii() or not seed.isdecimal() or len(seed) > 20:
        raise ValueError('朋友圈媒体密钥无效')
    length = min(len(data), 131072)
    stream = keystream(int(seed), length)
    return bytes(a ^ b for a, b in zip(data[:length], stream)) + data[length:]


def decrypt_image_full(data: bytes, seed: str) -> bytes:
    if not seed or not seed.isascii() or not seed.isdecimal() or len(seed) > 20:
        raise ValueError('朋友圈媒体密钥无效')
    stream = keystream(int(seed), len(data))
    return bytes(a ^ b for a, b in zip(data, stream))


def repair_partial_image(data: bytes, seed: str) -> bytes:
    """Older imports decoded only 128 KiB; decrypt their remaining tail."""
    if len(data) <= 131072:
        return data
    if not seed or not seed.isascii() or not seed.isdecimal() or len(seed) > 20:
        raise ValueError('朋友圈媒体密钥无效')
    stream = keystream(int(seed), len(data))
    return data[:131072] + bytes(a ^ b for a, b in zip(data[131072:], stream[131072:]))
