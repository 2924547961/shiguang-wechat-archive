"""Choose and remember where this Windows user keeps the Shiguang archive.

Only a folder pointer is stored in LocalAppData. Chat databases, exports,
provider secrets and WeChat keys stay outside the install/source directory.
"""
from __future__ import annotations

import os
from pathlib import Path

from .common import BASE, atomic_json, read_json


def location_file() -> Path:
    local = os.environ.get('LOCALAPPDATA')
    root = Path(local) if local else Path.home() / 'AppData' / 'Local'
    return root / 'Shiguang' / 'archive_location.json'


def default_archive(config: Path | None = None) -> Path:
    return (config or location_file()).parent / 'Archive'


def archive_choice(explicit: str | Path | None = None, *, base: Path = BASE,
                   config: Path | None = None) -> tuple[Path, bool]:
    """Return (archive path, first-run picker needed), preserving old installs."""
    if explicit:
        return Path(explicit).expanduser().resolve(), False
    pointer = config or location_file()
    saved = read_json(pointer)
    if isinstance(saved, dict) and isinstance(saved.get('path'), str) and saved['path'].strip():
        return Path(saved['path']).expanduser().resolve(), False
    legacy = Path(base).resolve() / '.shiguang'
    if legacy.is_dir():
        return legacy, False
    return default_archive(pointer).resolve(), True


def save_archive_choice(path: str | Path, *, config: Path | None = None) -> Path:
    value = Path(path).expanduser().resolve()
    if not value.is_dir():
        raise ValueError('请选择已经存在的归档文件夹。')
    if value.is_file():
        raise ValueError('归档位置必须是文件夹。')
    atomic_json(config or location_file(), {'path': str(value)})
    return value


def public_location(current: Path, *, config: Path | None = None) -> dict:
    pointer = config or location_file()
    saved = read_json(pointer)
    chosen = saved.get('path') if isinstance(saved, dict) else None
    return {'current': str(Path(current).resolve()),
            'next': str(Path(chosen).expanduser().resolve()) if isinstance(chosen, str) and chosen else str(Path(current).resolve()),
            'default': str(default_archive(pointer))}
