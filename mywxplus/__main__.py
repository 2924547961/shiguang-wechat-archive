"""Read-only diagnostics; never sends or mutates the client."""
from __future__ import annotations

import argparse
import json

from . import WeChat


def main():
    parser = argparse.ArgumentParser(prog="mywxplus")
    parser.add_argument("command", choices=["doctor"])
    args = parser.parse_args()
    if args.command == "doctor":
        wx = WeChat()
        try:
            count = len(wx.get_sessions())
            result = {"database": "ok", "account_detected": bool(wx.service.database.account_id),
                      "sample_session_count": count,
                      "features": {k: v.value for k, v in wx.features.items()}}
        except Exception as exc:
            result = {"database": "failed", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["database"] == "ok" else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
