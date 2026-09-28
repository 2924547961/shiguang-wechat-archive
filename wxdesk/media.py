from __future__ import annotations

import collections
import ctypes
import datetime as dt
import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import time
from pathlib import Path

from Crypto.Cipher import AES
from PIL import Image

from .common import check_cancel, inside, read_json

V1 = b"\x07\x08V1\x08\x07"
V2 = b"\x07\x08V2\x08\x07"
_derived_keys = {}


def derive_image_key(account_root, xor, valid):
    """Recover the account's V2 key from its directory suffix and image samples.

    The key stays in process memory. A candidate is accepted only after the
    encrypted first blocks of several local images decode to image headers.
    """
    name = Path(account_root).name
    match = re.fullmatch(r"(.+)_([0-9a-fA-F]{4})", name)
    if not match or xor is None:
        return None
    wxid, suffix = match.group(1), match.group(2).lower()
    cache_id = (name, xor)
    if cache_id in _derived_keys:
        key = _derived_keys[cache_id]
        return key if key and valid(key) else None
    wxid_bytes = wxid.encode()
    for uin in range(xor, 1 << 32, 256):
        uin_bytes = str(uin).encode()
        if hashlib.md5(uin_bytes).hexdigest()[:4] != suffix:
            continue
        key = hashlib.md5(uin_bytes + wxid_bytes).hexdigest()[:16].encode()
        if valid(key):
            _derived_keys[cache_id] = key
            return key
    _derived_keys[cache_id] = None
    return None


def image_type(data):
    for magic, ext in [(b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"),
                       (b"GIF87a", "gif"), (b"GIF89a", "gif"), (b"BM", "bmp"),
                       (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"), (b"wxgf", "wxgf"), (b"wxam", "wxgf")]:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def decode_dat(data, aes_key=None, xor_key=None):
    ext = image_type(data)
    if ext:
        return data, ext
    if data[:6] in {V1, V2}:
        if len(data) < 31:
            raise ValueError("图片文件不完整")
        key = b"cfcd208495d565ef" if data[:6] == V1 else aes_key
        if not key:
            raise ValueError("缺少当前版本的图片密钥")
        aes_len, xor_len = struct.unpack_from("<II", data, 6)
        padded = (aes_len // 16 + 1) * 16
        if padded > len(data) - 15:
            raise ValueError("图片加密段长度无效")
        head = AES.new(key, AES.MODE_ECB).decrypt(data[15:15 + padded])
        padding = head[-1]
        if not 1 <= padding <= 16 or head[-padding:] != bytes([padding]) * padding:
            raise ValueError("图片密钥校验未通过")
        head = head[:-padding]
        if len(head) != aes_len or not image_type(head):
            raise ValueError("图片内容校验未通过")
        rest = data[15 + padded:]
        xor_len = min(xor_len, len(rest))
        if xor_len and xor_key is None:
            if len(rest) >= 2 and rest[-2] ^ 0xFF == rest[-1] ^ 0xD9:
                xor_key = rest[-1] ^ 0xD9
            else:
                raise ValueError("缺少图片尾部解码参数")
        raw = head + (rest[:-xor_len] + bytes(b ^ xor_key for b in rest[-xor_len:]) if xor_len else rest)
        return raw, image_type(raw)
    for signature in [b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF"]:
        if len(data) >= len(signature):
            key = data[0] ^ signature[0]
            probe = bytes(b ^ key for b in data[:16])
            if image_type(probe):
                raw = bytes(b ^ key for b in data)
                return raw, image_type(raw)
    raise ValueError("尚未识别的图片编码")


def image_parameters(account_root, pids, cancel=None, seconds=60):
    """Read local image context only after an explicit sync; never log or persist the key."""
    templates, votes = [], collections.Counter()
    scanned = 0
    for path in (Path(account_root) / "msg" / "attach").rglob("*_t.dat"):
        check_cancel(cancel)
        try:
            with path.open("rb") as f:
                head = f.read(31)
                if len(head) < 31 or head[:6] != V2:
                    continue
                f.seek(-2, 2); tail = f.read(2)
            if tail[0] ^ 0xFF == tail[1] ^ 0xD9:
                votes[tail[1] ^ 0xD9] += 1
            if head[15:31] not in templates and len(templates) < 3:
                templates.append(head[15:31])
            scanned += 1
            if scanned >= 40:
                break
        except OSError:
            continue
    xor = votes.most_common(1)[0][0] if votes else None
    if not templates:
        return None, xor
    def valid(key):
        cipher = AES.new(key, AES.MODE_ECB)
        return all(image_type(cipher.decrypt(block)) for block in templates)
    # Earlier V2 releases used a fixed key; verify it before falling back to live memory.
    old = b"43e7d25eb1b9bb64"
    if valid(old):
        return old, xor
    derived = derive_image_key(account_root, xor, valid)
    if derived:
        return derived, xor
    if os.name != "nt":
        return None, xor
    from ctypes import wintypes as w
    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", w.DWORD), ("PartitionId", w.WORD), ("RegionSize", ctypes.c_size_t),
                    ("State", w.DWORD), ("Protect", w.DWORD), ("Type", w.DWORD)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; kernel.OpenProcess.restype = w.HANDLE
    kernel.VirtualQueryEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    kernel.VirtualQueryEx.restype = ctypes.c_size_t
    kernel.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    pattern = re.compile(rb"(?<![A-Za-z0-9])(?:[A-Za-z0-9]{32}|[A-Za-z0-9]{16})(?![A-Za-z0-9])")
    deadline, seen = time.monotonic() + seconds, set()
    for pid in pids:
        handle = kernel.OpenProcess(0x410, False, int(pid))
        if not handle:
            continue
        try:
            address = 0
            while time.monotonic() < deadline:
                check_cancel(cancel)
                mbi = MBI()
                if not kernel.VirtualQueryEx(handle, address, ctypes.byref(mbi), ctypes.sizeof(mbi)):
                    break
                base, size = mbi.BaseAddress or 0, mbi.RegionSize
                if not size or base + size <= address:
                    break
                address = base + size
                if mbi.State != 0x1000 or mbi.Protect & 0x100 or mbi.Protect & 0xFF not in {4, 8, 0x40, 0x80} or size > 64 * 1024 * 1024:
                    continue
                for offset in range(0, size, 2 * 1024 * 1024 - 32):
                    check_cancel(cancel)
                    if time.monotonic() >= deadline:
                        break
                    length = min(2 * 1024 * 1024, size - offset)
                    buf = ctypes.create_string_buffer(length); got = ctypes.c_size_t()
                    kernel.ReadProcessMemory(handle, base + offset, buf, length, ctypes.byref(got))
                    for match in pattern.finditer(buf.raw[:got.value]):
                        key = match[0][:16]
                        if key not in seen:
                            seen.add(key)
                            if valid(key):
                                return key, xor
        finally:
            kernel.CloseHandle(handle)
    return None, xor


class MediaResolver:
    def __init__(self, root, output, connection, aes_key=None, xor_key=None, cancel=None):
        self.root, self.output, self.c = Path(root).resolve(), Path(output), connection
        self.aes_key, self.xor_key, self.cancel = aes_key, xor_key, cancel
        self.cache = {}
        self.failures = {}
        self.emoji_index = None
        self.saved_emojis = read_json(self.output / 'emoji_index.json')

    def candidates(self, message):
        d, kind = message["detail"], message["kind"]
        month = dt.datetime.fromtimestamp(message["ts"]).strftime("%Y-%m")
        chat_hash = hashlib.md5(message["conversation_id"].encode()).hexdigest()
        raw_name = d.get("filename", "")
        name = Path(raw_name.replace("\\", "/")).name if raw_name else f"{message['local_id']}_{message['ts']}"
        found = []
        if kind == "image":
            base = self.root / "msg" / "attach" / chat_hash / month / "Img"
            stem = Path(name).stem
            found += [(base / (stem + suffix), "缩略图" if suffix == "_t.dat" else "已恢复") for suffix in ["_W.dat", "_h.dat", ".dat", "_t.dat"]]
        elif kind == "video":
            base = self.root / "msg" / "video" / month
            stem = Path(name).stem
            found += [(base / (stem + suffix), "已恢复") for suffix in ["_raw.mp4", ".mp4"]]
        elif kind == "file":
            found.append((self.root / "msg" / "file" / month / name, "已恢复"))
        for md5 in [d.get("rawmd5"), d.get("md5")]:
            if md5:
                for row in self.c.execute("SELECT path FROM media_map WHERE md5=? AND kind=?", (md5, kind)):
                    found.insert(0, (self.root / row[0], "已恢复"))
        if kind == "emoji":
            md5s = [v.lower() for v in [d.get("md5", ""), d.get("rawmd5", "")] if re.fullmatch(r"[0-9a-fA-F]{32}", v)]
            if md5s:
                if self.emoji_index is None:
                    self.emoji_index = {}
                    for folder in [self.root / "resource", self.root / "msg" / "emoji", *(self.root / 'cache').glob('*/Emoticon')]:
                        for path in folder.rglob("*"):
                            check_cancel(self.cancel)
                            if path.is_file() and re.search(r"[0-9a-fA-F]{32}", path.name):
                                self.emoji_index.setdefault(path.stem.lower(), path)
                for md5 in reversed(md5s):
                    if md5 in self.emoji_index:
                        found.insert(0, (self.emoji_index[md5], "已恢复"))
        if kind == 'image':
            # Never let an unordered hardlink thumbnail override an available original.
            priority={'_W.dat':0,'_h.dat':1}
            def rank(candidate):
                path,status=candidate;name=path.name
                return 4 if name.endswith('_t.dat') else 0 if name.endswith('_W.dat') else 1 if name.endswith('_h.dat') else 2
            found.sort(key=rank)
            found=[(p,'缩略图' if p.name.endswith('_t.dat') else status) for p,status in found]
        return found

    def save(self, path, image=False):
        path = inside(self.root, path)
        fingerprint = hashlib.sha256((str(path) + str(path.stat().st_mtime_ns)).encode()).hexdigest()[:32]
        if fingerprint in self.cache:
            return self.cache[fingerprint]
        self.output.mkdir(parents=True, exist_ok=True)
        # Media is immutable for this path + modification time. Reuse previous conversions.
        extensions = ['.png', '.jpg', '.gif', '.webp', '.bmp', '.tiff'] if image else [path.suffix.lower(), '.bin']
        for extension in extensions:
            cached = self.output / (fingerprint + extension)
            if cached.suffix != '.wxgf' and cached.is_file() and cached.stat().st_size:
                status = 'WXGF 首帧' if cached.with_suffix('.wxgf').exists() else '已恢复'
                self.cache[fingerprint] = (cached.relative_to(self.output.parent).as_posix(), status)
                return self.cache[fingerprint]
        if image:
            if path.stat().st_size > 150 * 1024 * 1024:
                raise ValueError("图片文件过大")
            raw, ext = decode_dat(path.read_bytes(), self.aes_key, self.xor_key)
            dest = self.output / (fingerprint + "." + ext)
            status = "已恢复"
            if ext == "wxgf":
                # WXGF contains HEVC NAL units. Preserve the source and extract a viewable first frame.
                dest.write_bytes(raw)
                marker = next((i for i in range(4, len(raw) - 6) if raw[i:i + 4] == b"\x00\x00\x00\x01" and ((raw[i + 4] >> 1) & 63) == 32), -1)
                if marker < 0:
                    raise ValueError("WXGF 图片编码尚无法解析")
                import imageio_ffmpeg
                converted = dest.with_suffix(".png")
                proc = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "hevc", "-i", "pipe:0", "-frames:v", "1", "-y", str(converted)],
                                      input=raw[marker:], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if proc.returncode or not converted.is_file():
                    raise ValueError("WXGF 图片解码失败")
                dest, status = converted, "WXGF 首帧"
            else:
                with Image.open(io.BytesIO(raw)) as img:
                    img.verify()
                dest.write_bytes(raw)
        else:
            ext = path.suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,12}", ext):
                ext = ".bin"
            dest = self.output / (fingerprint + ext)
            shutil.copy2(path, dest)
            status = "已恢复"
        self.cache[fingerprint] = (str(dest.relative_to(self.output.parent)).replace("\\", "/"), status)
        return self.cache[fingerprint]

    def resolve(self, message):
        kind = message["kind"]
        if kind not in {"image", "video", "audio", "emoji", "file"}:
            return "", ""
        if kind == 'emoji':
            for md5 in [message['detail'].get('md5', ''), message['detail'].get('rawmd5', '')]:
                filename = self.saved_emojis.get(md5.lower())
                if filename:
                    try:
                        path = inside(self.output, self.output / filename)
                        if path.is_file():
                            return path.relative_to(self.output.parent).as_posix(), '已恢复'
                    except ValueError:
                        pass
        if kind == "audio":
            row = self.c.execute("SELECT data FROM voice WHERE server_id=?", (message.get("server_id", ""),)).fetchone()
            if row and row[0]:
                try:
                    import pysilk
                    raw = bytes(row[0]); wav = raw if raw.startswith(b"RIFF") else pysilk.decode(raw, to_wav=True)
                    self.output.mkdir(parents=True, exist_ok=True)
                    dest = self.output / (hashlib.sha256(raw).hexdigest()[:32] + ".wav")
                    dest.write_bytes(wav)
                    return dest.relative_to(self.output.parent).as_posix(), "已恢复"
                except Exception:
                    return "", "语音解码失败"
            return "", "本机未保存语音"
        last = "本机未保存原文件"
        for path, status in self.candidates(message):
            check_cancel(self.cancel)
            fingerprint = None
            try:
                path = inside(self.root, path)
                if not path.is_file():
                    continue
                fingerprint = (str(path), path.stat().st_mtime_ns)
                if fingerprint in self.failures:
                    last = self.failures[fingerprint]
                    continue
                result, decoded_status = self.save(path, kind in {"image", "emoji"})
                return result, decoded_status if decoded_status != "已恢复" else status
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, Image.DecompressionBombError) as exc:
                last = str(exc) if isinstance(exc, ValueError) else "媒体文件无法读取"
                if kind == "emoji" and last == "尚未识别的图片编码":
                    last = "本地表情缓存编码暂不支持"
                if fingerprint is not None:
                    self.failures[fingerprint] = last
        return "", last

    def forward_media(self, message):
        detail = message["detail"]
        month = dt.datetime.fromtimestamp(message["ts"]).strftime("%Y-%m")
        chash = hashlib.md5(message["conversation_id"].encode()).hexdigest()
        rec_base = self.root / "msg" / "attach" / chash / month / "Rec"
        directory = detail.get("record_dir", "")
        if not directory and rec_base.is_dir():
            directory = next((p.name for p in rec_base.iterdir() if p.name.startswith(str(message["local_id"]) + "_")), "")
        def walk(items, prefix=""):
            for i, item in enumerate(items):
                check_cancel(self.cancel)
                key = f"{prefix}_{i}" if prefix else str(i)
                if item["kind"] == "forward":
                    walk(item.get("items", []), key)
                    continue
                kind = item["kind"]
                if kind not in {"image", "video", "file"}:
                    continue
                paths = []
                if directory:
                    base = rec_base / directory
                    if kind == "image":
                        paths = [base / "Img" / key, base / "Img" / (key + "_t")]
                    elif kind == "video":
                        paths = [base / "V" / (key + ".mp4")]
                    else:
                        paths = [base / "F" / key / Path(item.get("filename", "")).name]
                item["media_status"] = "本机未保存原文件"
                for p in paths:
                    try:
                        p = inside(self.root, p)
                        if p.is_file():
                            item["media_path"], item["media_status"] = self.save(p, kind == "image")
                            break
                    except Exception:
                        item["media_status"] = "媒体文件无法解析"
        walk(detail.get("items", []))
