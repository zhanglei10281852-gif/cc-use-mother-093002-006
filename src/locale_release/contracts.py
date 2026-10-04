"""课程版本与地区覆盖项的基础契约。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class BaseCourseVersion:
    entity_id: str
    display_name: str
    revision: int

    def __post_init__(self) -> None:
        if not self.entity_id or not self.display_name or self.revision < 1:
            raise ValueError("版本化实体信息不合法")


@dataclass(frozen=True)
class LocaleOverride:
    record_id: str
    entity_id: str
    category: str

    def __post_init__(self) -> None:
        if not self.record_id or not self.entity_id or not self.category:
            raise ValueError("关联记录信息不完整")


class ChangeType:
    """地区可提交的本地化变更类型。"""

    TERMINOLOGY = "术语覆盖"
    CASE = "案例替换"
    SCHEDULE = "课时调整"
    COMPLIANCE = "合规说明"

    ALL = (TERMINOLOGY, CASE, SCHEDULE, COMPLIANCE)


class ApproverRole:
    """参与按类型组合审批的角色。"""

    LANGUAGE_EXPERT = "语言专家"
    SUBJECT_OWNER = "学科负责人"
    PARTNER_SCHOOL = "合作院校"
