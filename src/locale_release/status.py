"""状态查询：地区继承差异、阻断原因与进程续接位置。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .release import Checkpoint


@dataclass(frozen=True)
class KeyDifference:
    """两个地区在同一内容键上的覆盖差异。"""

    target_key: str
    status_a: str
    status_b: str
    payload_a: str
    payload_b: str


@dataclass(frozen=True)
class RegionInheritanceDiff:
    """两个地区继承结果的直接比较。"""

    region_a: str
    region_b: str
    base_revision_a: int
    base_revision_b: int
    only_in_a: tuple[str, ...]
    only_in_b: tuple[str, ...]
    differing: tuple[KeyDifference, ...]

    @property
    def identical(self) -> bool:
        return (
            self.base_revision_a == self.base_revision_b
            and not self.only_in_a
            and not self.only_in_b
            and not self.differing
        )


@dataclass(frozen=True)
class BlockingReason:
    """阻断发布或推进的原因。"""

    code: str
    detail: str


@dataclass(frozen=True)
class ResumePosition:
    """重启的发布进程应续接的位置。"""

    process_id: str
    phase: str  # publish / rollback / completed
    next_action: str
    pending_regions: tuple[str, ...]
    last_checkpoint: Optional[Checkpoint]
