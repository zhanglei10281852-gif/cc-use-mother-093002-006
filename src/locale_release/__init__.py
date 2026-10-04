"""区域课程本地化发布领域包。"""
from .approval import ApprovalService, APPROVAL_RULES, required_roles
from .contracts import ApproverRole, BaseCourseVersion, ChangeType, LocaleOverride
from .content import ContentSnapshot, ContentUnit, DependencyVersion
from .localization import ChangeStatus, LocaleBranch, LocalizationChange
from .platform import LocalizationPlatform
from .release import (
    BatchPhase,
    PublishStage,
    ReleaseBatch,
    ReleaseEngine,
    ReleaseRecord,
    ReleaseRegistry,
    RollbackStage,
    TargetStatus,
)
from .status import ChangeDiff, compare_locales, resume_position, target_report

__all__ = [
    "ApprovalService",
    "APPROVAL_RULES",
    "required_roles",
    "ApproverRole",
    "BaseCourseVersion",
    "ChangeType",
    "LocaleOverride",
    "ContentSnapshot",
    "ContentUnit",
    "DependencyVersion",
    "ChangeStatus",
    "LocaleBranch",
    "LocalizationChange",
    "LocalizationPlatform",
    "BatchPhase",
    "PublishStage",
    "ReleaseBatch",
    "ReleaseEngine",
    "ReleaseRecord",
    "ReleaseRegistry",
    "RollbackStage",
    "TargetStatus",
    "ChangeDiff",
    "compare_locales",
    "resume_position",
    "target_report",
]
