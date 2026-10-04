"""区域课程本地化发布的核心领域模型。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from .contracts import BaseCourseVersion


class ChangeType(str, Enum):
    """地区可提交的本地化变更类型。"""

    TERMINOLOGY = "terminology"  # 术语覆盖
    CASE = "case"  # 案例替换
    SCHEDULE = "schedule"  # 课时调整
    COMPLIANCE = "compliance"  # 合规说明


class OverrideStatus(str, Enum):
    """覆盖项生命周期状态。"""

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"  # 上游修订引入的未解决差异
    SUPERSEDED = "superseded"  # 已被上游或新覆盖项取代


TERMINAL_OVERRIDE_STATES = frozenset(
    {OverrideStatus.REJECTED, OverrideStatus.SUPERSEDED}
)


@dataclass(frozen=True)
class ContentItem:
    """基础课程中的一个内容项。"""

    key: str
    change_type: ChangeType
    payload: str

    def __post_init__(self) -> None:
        if not self.key or not self.payload:
            raise ValueError("内容项信息不完整")


@dataclass(frozen=True)
class BaseCourseSnapshot:
    """经过批准的基础课程版本内容快照，是各地分支的继承源。"""

    version: BaseCourseVersion
    items: tuple[ContentItem, ...]
    approved: bool = False

    def item_map(self) -> dict[str, ContentItem]:
        return {item.key: item for item in self.items}

    def digest(self) -> str:
        canonical = [
            {"key": item.key, "type": item.change_type.value, "payload": item.payload}
            for item in sorted(self.items, key=lambda i: i.key)
        ]
        return stable_digest({"entity": self.version.entity_id, "items": canonical})


@dataclass(frozen=True)
class OverrideRequest:
    """地区针对某一内容键提交的本地化覆盖项。"""

    override_id: str
    region: str
    change_type: ChangeType
    target_key: str
    payload: str
    base_revision: int  # 覆盖项编写时所基于的基础版本
    revision: int = 1  # 覆盖项自身版本，编辑解决冲突时递增
    note: str = ""
    status: OverrideStatus = OverrideStatus.DRAFT

    def __post_init__(self) -> None:
        if not self.override_id or not self.region or not self.target_key:
            raise ValueError("覆盖项标识不完整")
        if self.base_revision < 1 or self.revision < 1:
            raise ValueError("覆盖项版本号不合法")


@dataclass
class RegionBranch:
    """地区分支：继承基础版本并持有本地覆盖项。"""

    region: str
    base_revision: int
    overrides: dict[str, OverrideRequest] = field(default_factory=dict)

    def active_overrides(self) -> list[OverrideRequest]:
        return [
            o for o in self.overrides.values() if o.status not in TERMINAL_OVERRIDE_STATES
        ]


@dataclass(frozen=True)
class MergedItem:
    """合并视图中的内容项：基础内容叠加已批准覆盖项后的结果。"""

    key: str
    change_type: ChangeType
    payload: str
    source: str  # "base" 或产生覆盖的 override_id


@dataclass(frozen=True)
class MergedView:
    """某地区当前继承状态下的有效内容视图。"""

    region: str
    base_revision: int
    items: tuple[MergedItem, ...]
    digest: str


def merged_view_digest(items: list[MergedItem]) -> str:
    canonical = [
        {"key": i.key, "type": i.change_type.value, "payload": i.payload}
        for i in sorted(items, key=lambda i: i.key)
    ]
    return stable_digest({"items": canonical})


def stable_digest(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
