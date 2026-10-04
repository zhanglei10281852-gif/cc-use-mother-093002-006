"""按变更类型组合的审批策略与确认有效性评估。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .models import ChangeType, OverrideRequest


class ApproverRole(str, Enum):
    """参与本地化审批的角色。"""

    LANGUAGE_EXPERT = "language_expert"  # 语言专家
    SUBJECT_LEAD = "subject_lead"  # 学科负责人
    PARTNER_SCHOOL = "partner_school"  # 合作院校


#: 每种变更类型要求的审批角色组合。
REQUIRED_ROLES: dict[ChangeType, frozenset[ApproverRole]] = {
    ChangeType.TERMINOLOGY: frozenset(
        {ApproverRole.LANGUAGE_EXPERT, ApproverRole.SUBJECT_LEAD}
    ),
    ChangeType.CASE: frozenset({ApproverRole.SUBJECT_LEAD, ApproverRole.PARTNER_SCHOOL}),
    ChangeType.SCHEDULE: frozenset({ApproverRole.PARTNER_SCHOOL}),
    ChangeType.COMPLIANCE: frozenset(
        {ApproverRole.LANGUAGE_EXPERT, ApproverRole.PARTNER_SCHOOL}
    ),
}

#: 审批确认的有效期，超时即视为过期确认。
DEFAULT_APPROVAL_TTL = timedelta(days=30)


@dataclass(frozen=True)
class Approval:
    """一条审批确认，绑定当时的覆盖项版本与基础版本。"""

    approval_id: str
    override_id: str
    role: ApproverRole
    approver: str
    approved_at: datetime
    override_revision: int
    base_revision: int


@dataclass(frozen=True)
class ApprovalIssue:
    """确认有效性评估发现的问题。"""

    code: str  # missing_role / expired / stale_override_revision / stale_base_revision
    detail: str


@dataclass(frozen=True)
class ApprovalEvaluation:
    override_id: str
    satisfied: bool
    issues: tuple[ApprovalIssue, ...]


def required_roles(change_type: ChangeType) -> frozenset[ApproverRole]:
    return REQUIRED_ROLES[change_type]


def evaluate_approvals(
    override: OverrideRequest,
    approvals: list[Approval],
    current_base_revision: int,
    now: datetime,
    ttl: timedelta = DEFAULT_APPROVAL_TTL,
) -> ApprovalEvaluation:
    """评估覆盖项的审批组合是否齐全且确认仍然有效。

    确认在三种情况下失效：超过有效期（过期确认）、覆盖项被重新编辑、
    基础版本向前推进（继承源变化）。
    """
    issues: list[ApprovalIssue] = []
    relevant = [a for a in approvals if a.override_id == override.override_id]
    for role in sorted(REQUIRED_ROLES[override.change_type], key=lambda r: r.value):
        candidates = [a for a in relevant if a.role == role]
        if not candidates:
            issues.append(ApprovalIssue("missing_role", f"缺少审批角色 {role.value}"))
            continue
        valid = [
            a
            for a in candidates
            if a.override_revision == override.revision
            and a.base_revision == current_base_revision
            and now - a.approved_at <= ttl
        ]
        if valid:
            continue
        # 角色缺少有效确认：以最新一条确认说明失效原因
        latest = max(candidates, key=lambda a: a.approved_at)
        if latest.override_revision != override.revision:
            issues.append(
                ApprovalIssue(
                    "stale_override_revision",
                    f"{role.value} 的确认基于覆盖项版本 {latest.override_revision}，"
                    f"当前为 {override.revision}",
                )
            )
        if latest.base_revision != current_base_revision:
            issues.append(
                ApprovalIssue(
                    "stale_base_revision",
                    f"{role.value} 的确认基于基础版本 R{latest.base_revision}，"
                    f"当前为 R{current_base_revision}",
                )
            )
        if now - latest.approved_at > ttl:
            issues.append(
                ApprovalIssue("expired", f"{role.value} 的确认已超过有效期")
            )
    return ApprovalEvaluation(override.override_id, not issues, tuple(issues))
