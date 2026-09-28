from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Session:
    username: str
    name: str
    unread: int = 0
    summary: str = ""
    last_time: int | None = None
    info: dict = field(default_factory=dict)
