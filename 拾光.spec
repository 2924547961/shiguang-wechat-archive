# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys
import importlib.util
from PyInstaller.utils.hooks import collect_all, collect_data_files

root = Path(SPECPATH)
qt_bin = Path(sys.prefix) / 'Library' / 'bin'
# This Conda environment stores Qt DLLs outside PySide6; make them visible to
# dependency analysis and ship them beside the frozen Python extension modules.
os.environ['PATH'] = str(qt_bin) + os.pathsep + os.environ.get('PATH', '')
datas = [(str(root / 'frontend'), 'frontend'),
         (str(root / 'licenses'), 'licenses'),
         (str(root / 'THIRD_PARTY_NOTICES.md'), '.'),
         (str(root / 'third_party' / 'RevokeMsgPatcher' / 'RevokeMsgPatcher.Assistant' / 'Data' / '2.1' / 'patch.json'),
          'third_party/RevokeMsgPatcher/RevokeMsgPatcher.Assistant/Data/2.1'),
         (str(root / 'mywxplus' / '_vendor' / 'wechatauto' / 'assets'),
          'mywxplus/_vendor/wechatauto/assets')]
binaries = []
hiddenimports = []
# Only these packages load non-Python assets at runtime. PyInstaller follows
# imports for the local mywxplus source and WinRT modules without collecting
# every optional module from those packages.
for package in ('imageio_ffmpeg', 'pysilk', 'sqlcipher3'):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
jieba_path = Path(importlib.util.find_spec('jieba').origin).parent
datas.append((str(jieba_path / 'dict.txt'), 'jieba'))
hiddenimports += ['wxdump', 'frida', 'qrcode', 'Crypto.Cipher.AES', 'zstandard', 'docx', 'jieba',
                  'backend.articles.service', 'requests', 'bs4', 'reportlab']

a = Analysis(
    [str(root / 'main.py')],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=['tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'pandas', 'matplotlib',
              'torch', 'tensorflow', 'scipy', 'pytest', 'IPython', 'pyarrow', 'sklearn',
              'paddle', 'jieba.lac_small', 'Cython', 'notebook', 'jupyter',
              'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.QtDesigner',
              'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtQuick3D'],
    noarchive=False,
)
# Conda's Library/bin may expose an entire Intel MKL installation to the
# binary scanner, although the bundled pip NumPy wheel uses OpenBLAS. None of
# the application binaries imports MKL; keep OpenBLAS and drop this unrelated
# family before creating the one-file archive.
a.binaries = [item for item in a.binaries if not Path(item[0]).name.lower().startswith('mkl_')]
a.datas = [item for item in a.datas if not item[0].replace('\\', '/').lower().startswith('jieba/lac_small/')]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    [], name='拾光-轻量版', icon=str(root / 'frontend' / 'app.ico'),
    debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
)
