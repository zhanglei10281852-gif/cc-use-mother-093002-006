import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from locale_release import (
    ChangeType,
    ContentSnapshot,
    ContentUnit,
    DependencyVersion,
    LocalizationChange,
    LocalizationPlatform,
)
from locale_release.approval import APPROVAL_RULES


def snapshot(revision, bodies):
    return ContentSnapshot(
        entity_id="E-CN-101",
        revision=revision,
        units=tuple(ContentUnit(uid, f"单元{uid}", body) for uid, body in bodies.items()),
        dependencies=(DependencyVersion("media", "2.1.0"), DependencyVersion("glossary", "1.0.0")),
    )


def fully_approve(platform, locale, change):
    for role in sorted(APPROVAL_RULES[change.change_type]):
        platform.approve_change(locale, change.change_id, role, approver_id=f"{role}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        clock_value = datetime(2026, 9, 1, 9, 0)
        platform = LocalizationPlatform(Path(tmp) / "state.json", clock=lambda: clock_value)

        platform.register_base_snapshot(
            snapshot(1, {"U1": "职业术语：技术员", "U2": "案例：本地工厂", "U3": "课时：16周"})
        )
        platform.onboard_locale("TH")
        platform.onboard_locale("VN")

        term_th = LocalizationChange("C-TH-1", ChangeType.TERMINOLOGY, "U1", "术语：技师（泰语区用法）", 1)
        sched_th = LocalizationChange("C-TH-2", ChangeType.SCHEDULE, "U3", "避开宋干节：15周", 1)
        term_vn = LocalizationChange("C-VN-1", ChangeType.TERMINOLOGY, "U1", "术语：技术员（越南语区用法）", 1)
        for locale, change in (("TH", term_th), ("TH", sched_th), ("VN", term_vn)):
            platform.submit_change(locale, change)
            fully_approve(platform, locale, change)
        platform.confirm_school("TH", "泰国合作院校")
        platform.confirm_school("VN", "越南合作院校")

        batch = platform.create_batch("B-2026-09", ("TH", "VN"))
        platform.run_batch("B-2026-09")

        report = {
            "th_status": platform.status("B-2026-09", "TH")["status"],
            "vn_status": platform.status("B-2026-09", "VN")["status"],
            "locked_digest": platform.status("B-2026-09", "TH")["locked_content_digest"][:12],
            "locale_diff": platform.compare("TH", "VN")["payload_diverged"],
            "th_resume": platform.resume("B-2026-09", "TH")["resume_from"],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
