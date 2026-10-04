"""地区本地化变更与覆盖集。

每个地区分支在某一基础版本之上维护一组变更（术语覆盖、案例替换、
课时调整、合规说明）。上游基础课程改版后，命中变更目标单元的
变更被标记为“过期”，必须重新确认（解决差异）后才允许发布。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Mapping

from .contracts import ChangeType
from .content import ContentSnapshot


class ChangeStatus:
    PROPOSED = "已提交"
    APPROVED = "已批准"
    REJECTED = "已驳回"
    STALE = "过期待确认"  # 上游变更影响到该变更，等待地区重新确认
    CONFLICTED = "存在未解决差异"

    RESOLVED = (APPROVED,)


@dataclass
class LocalizationChange:
    change_id: str
    change_type: str
    target_unit_id: str
    payload: str
    based_on_revision: int
    status: str = ChangeStatus.PROPOSED
    staleness_note: str = ""

    def __post_init__(self) -> None:
        if not self.change_id or self.change_type not in ChangeType.ALL:
            raise ValueError("本地化变更信息不合法")
        if not self.target_unit_id or not self.payload or self.based_on_revision < 1:
            raise ValueError("本地化变更信息不完整")

    @property
    def is_blocking(self) -> bool:
        return self.status in (ChangeStatus.STALE, ChangeStatus.CONFLICTED, ChangeStatus.REJECTED)

    @property
    def is_resolved(self) -> bool:
        """只有已批准的变更才是解决完毕、可参与发布的。"""
        return self.status == ChangeStatus.APPROVED

    def confirm_against(self, new_revision: int, note: str = "") -> None:
        """地区确认变更在新上游版本上仍然适用，回到已提交等待重新审批。"""
        if self.status not in (ChangeStatus.STALE, ChangeStatus.CONFLICTED):
            raise InvalidChangeState(f"变更 {self.change_id} 当前无需重新确认")
        self.status = ChangeStatus.PROPOSED
        self.based_on_revision = new_revision
        self.staleness_note = note

    def mark_conflicted(self, note: str) -> None:
        self.status = ChangeStatus.CONFLICTED
        self.staleness_note = note

    def digest(self) -> str:
        raw = json.dumps(
            {
                "change_id": self.change_id,
                "change_type": self.change_type,
                "target_unit_id": self.target_unit_id,
                "payload": self.payload,
                "based_on_revision": self.based_on_revision,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "change_id": self.change_id,
            "change_type": self.change_type,
            "target_unit_id": self.target_unit_id,
            "payload": self.payload,
            "based_on_revision": self.based_on_revision,
            "status": self.status,
            "staleness_note": self.staleness_note,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "LocalizationChange":
        return cls(**data)


class InvalidChangeState(Exception):
    """变更状态迁移不合法。"""


@dataclass
class LocaleBranch:
    """一个地区在基础课程之上的本地化分支。"""

    locale_code: str
    base_entity_id: str
    base_revision: int
    changes: dict[str, LocalizationChange] = field(default_factory=dict)
    pending_revision: int | None = None  # 已计算影响、但地区尚未继承的上游修订

    def __post_init__(self) -> None:
        if not self.locale_code or not self.base_entity_id or self.base_revision < 1:
            raise ValueError("地区分支信息不完整")

    def add_change(self, change: LocalizationChange) -> None:
        if change.change_id in self.changes:
            raise InvalidChangeState(f"变更 {change.change_id} 已存在")
        self.changes[change.change_id] = change

    def change(self, change_id: str) -> LocalizationChange:
        return self.changes[change_id]

    def changes_of_type(self, change_type: str) -> tuple[LocalizationChange, ...]:
        return tuple(c for c in self.changes.values() if c.change_type == change_type)

    def blocking_changes(self) -> tuple[LocalizationChange, ...]:
        return tuple(c for c in self.changes.values() if c.is_blocking)

    def pending_approval(self) -> tuple[LocalizationChange, ...]:
        return tuple(c for c in self.changes.values() if c.status == ChangeStatus.PROPOSED)

    def content_digest(self) -> str:
        """分支全部变更的指纹，供发布批次锁定“内容摘要”。"""
        raw = json.dumps(
            {
                "locale_code": self.locale_code,
                "base_entity_id": self.base_entity_id,
                "base_revision": self.base_revision,
                "changes": [c.to_dict() for c in sorted(self.changes.values(), key=lambda c: c.change_id)],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def apply_upstream(
        self, new_snapshot: ContentSnapshot, previous: ContentSnapshot | None
    ) -> tuple[LocalizationChange, ...]:
        """根据上游变更自动计算对本地分支的影响。

        分支不立即跳到新版本：命中的已提交/已批准变更转为“过期待确认”，
        目标单元被删除的转为“存在未解决差异”；差异全部解决并重新审批后，
        由 inherit_pending_revision 显式继承新修订。
        返回受影响的变更列表。
        """
        if new_snapshot.entity_id != self.base_entity_id:
            raise ValueError("不能对不同基础课程应用上游变更")
        if new_snapshot.revision <= self.base_revision:
            return ()
        affected_unit_ids = new_snapshot.changed_unit_ids(previous)
        current_units = {u.unit_id for u in new_snapshot.units}
        affected: list[LocalizationChange] = []
        for change in self.changes.values():
            if change.target_unit_id not in affected_unit_ids:
                continue
            if change.target_unit_id not in current_units:
                change.mark_conflicted(
                    f"上游修订 {new_snapshot.revision} 删除了单元 {change.target_unit_id}，需解决差异"
                )
            elif change.status in (ChangeStatus.PROPOSED, ChangeStatus.APPROVED):
                change.status = ChangeStatus.STALE
                change.staleness_note = (
                    f"上游修订 {new_snapshot.revision} 修改了单元 {change.target_unit_id}，需重新确认"
                )
            affected.append(change)
        self.pending_revision = new_snapshot.revision
        return tuple(affected)

    def has_pending_upstream(self) -> bool:
        return self.pending_revision is not None and self.pending_revision != self.base_revision

    def can_inherit_pending(self) -> bool:
        """差异均已解决（全部批准、无阻断状态）才允许继承。"""
        return self.has_pending_upstream() and all(c.is_resolved for c in self.changes.values())

    def inherit_pending_revision(self) -> int:
        if not self.has_pending_upstream():
            raise InvalidChangeState("没有待继承的上游修订")
        if not self.can_inherit_pending():
            raise InvalidChangeState("仍存在未解决差异，不能继承上游修订")
        new_revision = self.pending_revision
        self.base_revision = new_revision  # type: ignore[assignment]
        self.pending_revision = None
        # 已批准变更所基于的修订一并前移
        for change in self.changes.values():
            change.based_on_revision = new_revision
        return new_revision

    def to_dict(self) -> dict:
        return {
            "locale_code": self.locale_code,
            "base_entity_id": self.base_entity_id,
            "base_revision": self.base_revision,
            "pending_revision": self.pending_revision,
            "changes": [c.to_dict() for c in self.changes.values()],
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "LocaleBranch":
        branch = cls(
            locale_code=data["locale_code"],
            base_entity_id=data["base_entity_id"],
            base_revision=data["base_revision"],
            pending_revision=data.get("pending_revision"),
        )
        for raw in data["changes"]:
            branch.add_change(LocalizationChange.from_dict(raw))
        return branch
