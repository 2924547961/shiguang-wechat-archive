from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    executable_dir = Path(sys.executable).resolve().parent
    # The development dist folder should reuse this project's existing archive.
    BASE = executable_dir.parent if executable_dir.name.lower() == "dist" and (executable_dir.parent / ".shiguang").is_dir() else executable_dir
else:
    BASE = Path(__file__).resolve().parent.parent
STATIC = (Path(sys._MEIPASS) / "frontend") if getattr(sys, "frozen", False) else BASE / "frontend"
STATE = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))) / 'Shiguang' / 'Archive'


class Cancelled(Exception):
    pass


def check_cancel(cancel=None):
    if cancel is not None and cancel.is_set():
        raise Cancelled("任务已取消；原有归档仍然保留。")


def identity(account: str) -> str:
    return hashlib.sha256(account.encode("utf-8")).hexdigest()[:24]


def self_username(account: str) -> str:
    return re.sub(r"_[0-9a-fA-F]{4}$", "", account)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


def inside(root: Path, child: Path) -> Path:
    root, child = root.resolve(), child.resolve()
    if not child.is_relative_to(root):
        raise ValueError("路径不在允许的文件夹内。")
    return child


def safe_name(value: str, limit=65) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(value)).strip(" .")[:limit]
    if not text or text.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}:
        text = "_" + (text or "未命名")
    return text


def clean_error(error) -> str:
    # Never display database/image keys, even if a third-party exception includes them.
    return re.sub(r"(?i)\b[0-9a-f]{32,}\b", "[已隐藏]", str(error))[:1000]
