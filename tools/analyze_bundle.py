"""Show the largest pieces of a PyInstaller single-file executable."""
import collections
import re
import subprocess
import sys
from pathlib import Path

exe = Path(sys.argv[1])
result = subprocess.run([sys.executable, '-m', 'PyInstaller.utils.cliutils.archive_viewer',
                         '-l', str(exe)], capture_output=True, text=True, errors='replace', check=True)
items = []
for line in result.stdout.splitlines():
    match = re.match(r"\s*\d+,\s*(\d+),\s*(\d+),\s*\d+,\s*'[^']+',\s*'([^']+)'", line)
    if match:
        compressed, raw, name = match.groups()
        items.append((int(compressed), int(raw), name))
groups = collections.Counter()
for compressed, raw, name in items:
    parts = name.replace('\\', '/').split('/')
    group = parts[0] if len(parts) > 1 else ('Qt6 DLLs' if name.startswith('Qt6') else 'root files')
    groups[group] += compressed
print('Total archive entries:', len(items))
print('Largest groups (MiB):')
for group, size in groups.most_common(20):
    print(f'{size / 1048576:7.1f}  {group}')
print('Largest files (MiB):')
for compressed, raw, name in sorted(items, reverse=True)[:30]:
    print(f'{compressed / 1048576:7.1f}  {name}')
