"""Read-only structural UI Automation diagnostics."""
from __future__ import annotations

from collections import Counter


def inspect_ui_tree(max_nodes: int = 2000, max_depth: int = 40) -> dict:
    if max_nodes <= 0 or max_depth < 0:
        raise ValueError("max_nodes 必须大于 0，max_depth 不能为负")
    from ._vendor.wechatauto.uia_driver import WeChatUIA
    root = WeChatUIA()._find_main()
    if root is None:
        return {"root_found": False, "reason": "mmui::MainWindow 未暴露"}
    classes: Counter[str] = Counter()
    types: Counter[str] = Counter()
    ids: Counter[str] = Counter()
    stack = [(root, 0)]
    errors = []
    count = depth_seen = 0
    while stack and count < max_nodes:
        control, depth = stack.pop()
        count += 1
        depth_seen = max(depth_seen, depth)
        try:
            classes[control.ClassName or ""] += 1
            types[control.ControlTypeName or ""] += 1
            aid = control.AutomationId or ""
            if aid:
                ids[aid] += 1
            if depth < max_depth:
                stack.extend((child, depth + 1) for child in reversed(control.GetChildren()))
        except Exception as exc:
            errors.append(type(exc).__name__)
    return {
        "root_found": True,
        "root_class": root.ClassName,
        "node_count": count,
        "max_depth": depth_seen,
        "truncated": bool(stack),
        "read_errors": errors,
        "class_counts": dict(classes),
        "control_type_counts": dict(types),
        "automation_ids": dict(ids),
    }
