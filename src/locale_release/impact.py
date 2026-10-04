"""上游基础版本变更对各地分支的影响分析。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import (
    TERMINAL_OVERRIDE_STATES,
    BaseCourseSnapshot,
    OverrideStatus,
    RegionBranch,
)


class ImpactKind(str, Enum):
    CONTENT_CONFLICT = "content_conflict"  # 上游改动了被覆盖的内容键
    APPROVAL_STALE = "approval_stale"  # 基础版本推进使既有确认失效


@dataclass(frozen=True)
class OverrideImpact:
    override_id: str
    region: str
    target_key: str
    kind: ImpactKind
    detail: str


@dataclass(frozen=True)
class ImpactReport:
    """一个地区分支在一次基础版本推进中受到的影响。"""

    region: str
    from_revision: int
    to_revision: int
    impacts: tuple[OverrideImpact, ...]

    @property
    def conflicted_override_ids(self) -> tuple[str, ...]:
        return tuple(
            i.override_id for i in self.impacts if i.kind == ImpactKind.CONTENT_CONFLICT
        )

    @property
    def blocked(self) -> bool:
        return bool(self.impacts)


def changed_content_keys(
    old: BaseCourseSnapshot, new: BaseCourseSnapshot
) -> set[str]:
    """上游版本中被修改或删除的内容键（新增键不影响既有覆盖项）。"""
    old_map, new_map = old.item_map(), new.item_map()
    changed: set[str] = set()
    for key, item in old_map.items():
        other = new_map.get(key)
        if (
            other is None
            or other.payload != item.payload
            or other.change_type != item.change_type
        ):
            changed.add(key)
    return changed


def compute_branch_impact(
    branch: RegionBranch,
    old: BaseCourseSnapshot,
    new: BaseCourseSnapshot,
    override_ids_with_approvals: set[str],
) -> ImpactReport:
    """计算基础版本从 old 推进到 new 对单个地区分支的影响。"""
    changed = changed_content_keys(old, new)
    impacts: list[OverrideImpact] = []
    for ov in branch.overrides.values():
        if ov.status in TERMINAL_OVERRIDE_STATES:
            continue
        if ov.target_key in changed:
            impacts.append(
                OverrideImpact(
                    ov.override_id,
                    branch.region,
                    ov.target_key,
                    ImpactKind.CONTENT_CONFLICT,
                    f"基础版本 R{new.version.revision} 修订了内容键 {ov.target_key}",
                )
            )
        elif (
            ov.status in (OverrideStatus.APPROVED, OverrideStatus.PENDING_APPROVAL)
            and ov.override_id in override_ids_with_approvals
        ):
            impacts.append(
                OverrideImpact(
                    ov.override_id,
                    branch.region,
                    ov.target_key,
                    ImpactKind.APPROVAL_STALE,
                    f"基础版本推进到 R{new.version.revision}，既有确认需重新评估",
                )
            )
    return ImpactReport(
        branch.region, old.version.revision, new.version.revision, tuple(impacts)
    )
