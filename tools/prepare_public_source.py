"""Copy application source into a clean directory before Git publication.

Run from this checkout with an empty destination outside the project. The
allowlist excludes chats, database keys, exports, logs and compiled binaries.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    '.gitignore', 'LICENSE', 'README.md', 'THIRD_PARTY_NOTICES.md',
    'main.py', 'wxdump.py', 'wxdump_gui.py', 'wxdump_gui_legacy.py',
    'requirements.txt', 'pytest.ini', '拾光.spec', '打包单文件.ps1',
    '启动拾光.cmd', '启动拾光.vbs',
}
SOURCE_DIRS = ('backend', 'frontend', 'wxdesk', 'mywxplus', 'tests', 'docs',
               'licenses', 'third_party/RevokeMsgPatcher', 'tools')
SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', '.venv', '.shiguang',
             'build', 'dist', 'backups', 'logs', 'wechatauto_logs',
             'example_output', 'multi_batch_example'}
SKIP_SUFFIXES = {'.exe', '.dll', '.pdb', '.zip', '.rar', '.db', '.sqlite', '.tmp',
                 '.log', '.pyc', '.pyo'}
SKIP_NAMES = {'_mk_testraw.py', '.env', 'automation_secret.json'}


def wanted(relative: Path) -> bool:
    if any(part in SKIP_DIRS or part.startswith('export_') for part in relative.parts[:-1]):
        return False
    name = relative.name
    if name in SKIP_NAMES or name.startswith(('keys_', 'live2.', '.env.')):
        return False
    if name.endswith(('.raw.json', '.verified.json')) or relative.suffix.lower() in SKIP_SUFFIXES:
        return False
    return True


def copy_source(destination: Path) -> list[Path]:
    destination = destination.expanduser().resolve()
    if destination == ROOT or ROOT.is_relative_to(destination) or destination.is_relative_to(ROOT):
        raise ValueError('公开仓库必须与工作项目分开，避免意外包含运行数据。')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('目标目录必须为空。')
    destination.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in sorted(ROOT_FILES):
        src = ROOT / name
        if not src.is_file():
            raise FileNotFoundError(src)
        dst = destination / name
        shutil.copy2(src, dst)
        copied.append(dst)
    for folder in SOURCE_DIRS:
        source = ROOT / folder
        for src in source.rglob('*'):
            if not src.is_file() or src.is_symlink():
                continue
            relative = src.relative_to(ROOT)
            if not wanted(relative):
                continue
            dst = destination / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(dst)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description='Prepare a data-free public source checkout')
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    files = copy_source(args.destination)
    total = sum(path.stat().st_size for path in files)
    print(f'Prepared {len(files)} source files ({total / 1024 / 1024:.1f} MiB) at {args.destination.resolve()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
