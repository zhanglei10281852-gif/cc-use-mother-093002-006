"""测试共享辅助：假时钟、基础快照与预置服务。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from locale_release import (  # noqa: E402
    BaseCourseSnapshot,
    BaseCourseVersion,
    ChangeType,
    ContentItem,
    LocalizationService,
)

BASE_ITEMS = (
    ContentItem("term:薪资谈判", ChangeType.TERMINOLOGY, "薪资谈判"),
    ContentItem("case:职场对话", ChangeType.CASE, "总部案例文本"),
    ContentItem("lesson:3", ChangeType.SCHEDULE, "45分钟"),
)


class FakeClock:
    def __init__(self, start=None):
        self.now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def base_snapshot(revision, items=None):
    return BaseCourseSnapshot(
        BaseCourseVersion("COURSE-ZH", "国际中文职业课程", revision),
        items if items is not None else BASE_ITEMS,
        approved=True,
    )


def make_service():
    clock = FakeClock()
    service = LocalizationService(clock=clock)
    service.publish_base_snapshot(base_snapshot(1))
    return service, clock


def approve_and_finalize(service, override_id, role_approver_pairs):
    for index, (role, approver) in enumerate(role_approver_pairs):
        service.record_approval(f"AP-{override_id}-{index}", override_id, role, approver)
    return service.finalize_override(override_id)
