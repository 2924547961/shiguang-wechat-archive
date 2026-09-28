"""Look for WeChat article session URLs in a user-selected local cache folder."""

from __future__ import annotations

from pathlib import Path
import re
import threading
from urllib.parse import parse_qs, urlparse

from .downloader import AUTH_KEYS


URL_PATTERN = re.compile(rb"https?://mp\.weixin\.qq\.com/[^\x00-\x20\"'<>]{10,4096}", re.I)
UTF16_PREFIX = "https://mp.weixin.qq.com/".encode("utf-16le")


def find_session_params(root: Path, stop: threading.Event | None = None,
                        max_files: int = 1500, max_file_size: int = 2_000_000) -> tuple[dict[str, str], Path | None]:
    """Read only recent small files under root; return first complete session URL.

    The caller must choose root. No credentials are saved or logged here.
    """
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("缓存目录不存在")
    candidates = []
    for path in root.rglob("*"):
        if stop and stop.is_set():
            return {}, None
        try:
            if path.is_file():
                stat = path.stat()
                if 0 < stat.st_size <= max_file_size:
                    candidates.append((stat.st_mtime, path))
        except (OSError, PermissionError):
            continue
    candidates.sort(reverse=True)
    for _, path in candidates[:max_files]:
        if stop and stop.is_set():
            return {}, None
        try:
            data = path.read_bytes()
        except (OSError, PermissionError):
            continue
        urls = [m.group().decode("utf-8", "ignore") for m in URL_PATTERN.finditer(data)]
        start = 0
        while (index := data.find(UTF16_PREFIX, start)) != -1:
            tail = data[index:index + 8192]
            decoded = tail.decode("utf-16le", "ignore")
            match = re.match(r"https://mp\.weixin\.qq\.com/[^\s\x00\"'<>]+", decoded)
            if match:
                urls.append(match.group())
            start = index + len(UTF16_PREFIX)
        for url in urls:
            url = url.replace("&amp;", "&").rstrip(".,);]}")
            parsed = urlparse(url)
            if parsed.hostname != "mp.weixin.qq.com":
                continue
            query = parse_qs(parsed.query)
            found = {key: query[key][-1] for key in AUTH_KEYS if query.get(key)}
            if all(found.get(key) for key in ("uin", "key", "pass_ticket")):
                return found, path
    return {}, None
