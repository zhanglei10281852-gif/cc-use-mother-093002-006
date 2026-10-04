"""按变更类型组合的审批与合作院校确认。

不同变更类型需要不同角色组合的审批；变更被上游影响而过期后，
原有审批全部作废，重新确认后必须再次走完整审批。
合作院校对发布内容的确认带有有效期与内容指纹，过期或内容变动即失效。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Mapping

from .contracts import ApproverRole, ChangeType
from .localization import ChangeStatus, LocaleBranch, LocalizationChange

# 各变更类型要求的审批角色组合
APPROVAL_RULES: dict[str, frozenset[str]] = {
    ChangeType.TERMINOLOGY: frozenset({ApproverRole.LANGUAGE_EXPERT, ApproverRole.PARTNER_SCHOOL}),
    ChangeType.CASE: frozenset({ApproverRole.SUBJECT_OWNER, ApproverRole.LANGUAGE_EXPERT}),
    ChangeType.SCHEDULE: frozenset({ApproverRole.SUBJECT_OWNER, ApproverRole.PARTNER_SCHOOL}),
    ChangeType.COMPLIANCE: frozenset(
        {ApproverRole.LANGUAGE_EXPERT, ApproverRole.SUBJECT_OWNER, ApproverRole.PARTNER_SCHOOL}
    ),
}

DEFAULT_CONFIRMATION_TTL = timedelta(days=30)


class ApprovalError(Exception):
    """审批操作不合法。"""


def required_roles(change_type: str) -> frozenset[str]:
    return APPROVAL_RULES[change_type]


@dataclass(frozen=True)
class ApprovalRecord:
    change_id: str
    role: str
    approver_id: str
    decided_at: datetime

    def to_dict(self) -> dict:
        return {
            "change_id": self.change_id,
            "role": self.role,
            "approver_id": self.approver_id,
            "decided_at": self.decided_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "ApprovalRecord":
        return cls(
            change_id=data["change_id"],
            role=data["role"],
            approver_id=data["approver_id"],
            decided_at=datetime.fromisoformat(data["decided_at"]),
        )


class ApprovalLedger:
    """记录每条变更已经获得的审批。"""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ApprovalRecord] = {}

    def record(self, rec: ApprovalRecord) -> None:
        self._records[(rec.change_id, rec.role)] = rec

    def roles_for(self, change_id: str) -> frozenset[str]:
        return frozenset(role for (cid, role) in self._records if cid == change_id)

    def revoke_change(self, change_id: str) -> None:
        for key in [k for k in self._records if k[0] == change_id]:
            del self._records[key]

    def to_dict(self) -> dict:
        return {"records": [r.to_dict() for r in self._records.values()]}

    @classmethod
    def from_dict(cls, data: Mapping) -> "ApprovalLedger":
        ledger = cls()
        for raw in data.get("records", ()):
            ledger.record(ApprovalRecord.from_dict(raw))
        return ledger


@dataclass(frozen=True)
class SchoolConfirmation:
    locale_code: str
    content_digest: str
    base_revision: int
    confirmed_by: str
    valid_until: datetime

    def covers(self, digest: str, base_revision: int) -> bool:
        return self.content_digest == digest and self.base_revision == base_revision

    def is_valid(self, digest: str, base_revision: int, now: datetime) -> bool:
        return self.covers(digest, base_revision) and now <= self.valid_until

    def to_dict(self) -> dict:
        return {
            "locale_code": self.locale_code,
            "content_digest": self.content_digest,
            "base_revision": self.base_revision,
            "confirmed_by": self.confirmed_by,
            "valid_until": self.valid_until.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "SchoolConfirmation":
        return cls(
            locale_code=data["locale_code"],
            content_digest=data["content_digest"],
            base_revision=data["base_revision"],
            confirmed_by=data["confirmed_by"],
            valid_until=datetime.fromisoformat(data["valid_until"]),
        )


class ApprovalService:
    def __init__(
        self,
        ledger: ApprovalLedger | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.ledger = ledger or ApprovalLedger()
        self._clock = clock
        self._confirmations: dict[str, SchoolConfirmation] = {}

    # ---- 单条变更审批 ----

    def approve(self, branch: LocaleBranch, change_id: str, role: str, approver_id: str) -> ApprovalRecord:
        change = branch.change(change_id)
        if change.status == ChangeStatus.STALE:
            raise ApprovalError(f"变更 {change_id} 已过期，须先重新确认差异")
        if change.status == ChangeStatus.CONFLICTED:
            raise ApprovalError(f"变更 {change_id} 存在未解决差异，不能审批")
        if change.status == ChangeStatus.REJECTED:
            raise ApprovalError(f"变更 {change_id} 已被驳回")
        if role not in required_roles(change.change_type):
            raise ApprovalError(f"{role} 不是 {change.change_type} 要求的审批角色")
        rec = ApprovalRecord(change_id, role, approver_id, self._clock())
        self.ledger.record(rec)
        if self.ledger.roles_for(change_id) >= required_roles(change.change_type):
            change.status = ChangeStatus.APPROVED
        return rec

    def reject(self, branch: LocaleBranch, change_id: str, role: str, approver_id: str) -> None:
        change = branch.change(change_id)
        if role not in required_roles(change.change_type):
            raise ApprovalError(f"{role} 无权审批 {change.change_type}")
        change.status = ChangeStatus.REJECTED
        self.ledger.revoke_change(change_id)

    def missing_roles(self, change: LocalizationChange) -> frozenset[str]:
        if change.status == ChangeStatus.APPROVED:
            return frozenset()
        return required_roles(change.change_type) - self.ledger.roles_for(change.change_id)

    def invalidate_stale_approvals(self, changes: tuple[LocalizationChange, ...]) -> None:
        """上游改版使变更过期时，清除它们既有的审批。"""
        for change in changes:
            self.ledger.revoke_change(change.change_id)

    # ---- 合作院校发布确认 ----

    def confirm_school(
        self,
        branch: LocaleBranch,
        confirmed_by: str,
        ttl: timedelta = DEFAULT_CONFIRMATION_TTL,
    ) -> SchoolConfirmation:
        confirmation = SchoolConfirmation(
            locale_code=branch.locale_code,
            content_digest=branch.content_digest(),
            base_revision=branch.base_revision,
            confirmed_by=confirmed_by,
            valid_until=self._clock() + ttl,
        )
        self._confirmations[branch.locale_code] = confirmation
        return confirmation

    def confirmation(self, locale_code: str) -> SchoolConfirmation | None:
        return self._confirmations.get(locale_code)

    def school_confirmation_state(self, branch: LocaleBranch) -> tuple[bool, str]:
        """返回（是否有效, 原因）。"""
        confirmation = self._confirmations.get(branch.locale_code)
        digest = branch.content_digest()
        if confirmation is None:
            return False, "缺少合作院校发布确认"
        if not confirmation.covers(digest, branch.base_revision):
            return False, "内容在上次院校确认后发生变化，需重新确认"
        if self._clock() > confirmation.valid_until:
            return False, f"院校确认已于 {confirmation.valid_until.isoformat()} 过期"
        return True, ""

    def to_dict(self) -> dict:
        return {
            "ledger": self.ledger.to_dict(),
            "confirmations": [c.to_dict() for c in self._confirmations.values()],
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping,
        clock: Callable[[], datetime] = datetime.now,
    ) -> "ApprovalService":
        service = cls(ledger=ApprovalLedger.from_dict(data.get("ledger", {})), clock=clock)
        for raw in data.get("confirmations", ()):
            conf = SchoolConfirmation.from_dict(raw)
            service._confirmations[conf.locale_code] = conf
        return service
