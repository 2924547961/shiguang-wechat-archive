from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class FeatureStatus(str, Enum):
    SUPPORTED = "supported"
    EXPERIMENTAL = "experimental"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SafetyConfig:
    allow_contact_mutation: bool = False
    allow_group_mutation: bool = False
    allow_moment_mutation: bool = False
    allow_send: bool = True


@dataclass(frozen=True)
class AppConfig:
    account: str | None = None
    db_dir: Path | None = None
    cache_dir: Path | None = None
    poll_interval: float = 1.0
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    prefer_uia: bool = True
    locale: str = "zh_CN"
