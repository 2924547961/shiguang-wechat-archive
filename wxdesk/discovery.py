from __future__ import annotations

import os
from pathlib import Path

import psutil

from .common import BASE, STATE, identity, read_json, self_username


def account_from_db_path(path):
    p = Path(path)
    lowered = [s.casefold() for s in p.parts]
    if "db_storage" not in lowered:
        return None
    i = lowered.index("db_storage")
    if i < 2:
        return None
    dbdir = Path(*p.parts[:i + 1])
    return {"account": p.parts[i - 1], "dbdir": str(dbdir), "root": str(dbdir.parent.parent)}


def legacy_archives(base=BASE):
    result = []
    for directory in Path(base).glob("export_*"):
        summary = directory / "_summary.txt"
        if not summary.is_file():
            continue
        try:
            # The directory name is NOT an account identity. Old exports may be misnamed.
            account, dbdir = "", ""
            for line in summary.read_text("utf-8-sig").splitlines()[:12]:
                if line.startswith("账号:"):
                    account = line.split(":", 1)[1].strip()
                if line.startswith("数据库目录:"):
                    dbdir = line.split(":", 1)[1].strip()
            if account and list(directory.glob("index_*.csv")):
                result.append({"account": account, "path": str(directory), "dbdir": dbdir,
                               "modified": summary.stat().st_mtime})
        except OSError:
            continue
    return sorted(result, key=lambda r: r["modified"], reverse=True)


def find_verified(account, dbdir, base=BASE, state=STATE):
    matches = []
    candidates = list(Path(base).glob("*.verified.json"))
    candidates += list((Path(state) / "accounts" / identity(account)).glob("*.verified.json"))
    for path in candidates:
        d = read_json(path)
        if d.get("account") != account or not d.get("keys"):
            continue
        if os.path.normcase(os.path.abspath(d.get("dbdir", ""))) != os.path.normcase(os.path.abspath(dbdir)):
            continue
        matches.append((len(d["keys"]), path.stat().st_mtime, path))
    return str(max(matches)[2]) if matches else None


def discover(data_root="", base=BASE, state=STATE):
    active = {}
    processes = []
    denied = 0
    for p in psutil.process_iter(["pid", "name", "create_time"]):
        if (p.info.get("name") or "").casefold() != "weixin.exe":
            continue
        processes.append(p.info["pid"])
        try:
            for opened in p.open_files():
                a = account_from_db_path(opened.path)
                if not a or not Path(a["dbdir"]).is_dir():
                    continue
                key = os.path.normcase(a["dbdir"])
                if key not in active:
                    active[key] = dict(a, pids=[], active=True)
                if p.pid not in active[key]["pids"]:
                    active[key]["pids"].append(p.pid)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            denied += 1
    accounts = list(active.values())
    # A saved archive remains usable offline, but is never presented as a logged-in account.
    known = {a["account"] for a in accounts}
    for directory in (Path(state) / "accounts").glob("*"):
        metadata = read_json(directory / "account.json")
        if metadata.get("account") and metadata["account"] not in known and (directory / "archive.sqlite").is_file():
            accounts.append(dict(metadata, active=False, pids=[]))
            known.add(metadata["account"])
    archives = legacy_archives(base)
    for archived in archives:
        if archived["account"] not in known:
            accounts.append({"account": archived["account"], "dbdir": archived["dbdir"],
                             "root": str(Path(archived["dbdir"]).parent.parent), "active": False, "pids": []})
            known.add(archived["account"])
    for a in accounts:
        a["id"] = identity(a["account"])
        a["username"] = self_username(a["account"])
        a["display_name"] = a.get("display_name") or a["username"]
        archive_dir = Path(state) / "accounts" / a["id"]
        meta = read_json(archive_dir / "account.json")
        if meta.get("display_name"):
            a["display_name"] = meta["display_name"]
        a["has_archive"] = (archive_dir / "archive.sqlite").is_file()
        a["legacy"] = next((r["path"] for r in archives if r["account"] == a["account"]), None)
        a["verified"] = bool(find_verified(a["account"], a["dbdir"], base, state))
        try:
            a["database_count"] = sum(1 for _ in Path(a["dbdir"]).rglob("*.db")) if a["active"] else 0
        except OSError:
            a["database_count"] = 0
    return {"accounts": accounts, "process_count": len(processes), "access_denied": denied,
            "active_count": len(active), "scanned_root": data_root}
