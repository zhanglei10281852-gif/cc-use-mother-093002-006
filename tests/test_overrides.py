import unittest

from helpers import approve_and_finalize, base_snapshot, make_service
from locale_release import (
    ApproverRole,
    ChangeType,
    DomainError,
    OverrideStatus,
)


class OverrideSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        self.service.open_branch("SG")

    def test_submit_override_registers_pending_request(self):
        override = self.service.submit_override(
            "OV-1", "SG", ChangeType.TERMINOLOGY, "term:薪资谈判", "薪酬商谈"
        )
        self.assertEqual(override.status, OverrideStatus.PENDING_APPROVAL)
        self.assertEqual(override.base_revision, 1)
        # 覆盖项同时登记到基础契约的关联记录中
        record = self.service.repo.override_registry["OV-1"]
        self.assertEqual(record.entity_id, "COURSE-ZH")
        self.assertEqual(record.category, "terminology")

    def test_submit_override_rejects_unknown_target_key(self):
        with self.assertRaises(DomainError):
            self.service.submit_override(
                "OV-2", "SG", ChangeType.TERMINOLOGY, "term:不存在", "x"
            )

    def test_submit_override_rejects_duplicate_active_key(self):
        self.service.submit_override(
            "OV-3", "SG", ChangeType.TERMINOLOGY, "term:薪资谈判", "薪酬商谈"
        )
        with self.assertRaises(DomainError):
            self.service.submit_override(
                "OV-4", "SG", ChangeType.TERMINOLOGY, "term:薪资谈判", "工资协商"
            )

    def test_open_branch_twice_is_rejected(self):
        with self.assertRaises(DomainError):
            self.service.open_branch("SG")

    def test_unapproved_base_snapshot_cannot_be_source(self):
        from dataclasses import replace

        service, _ = make_service()
        snapshot = replace(base_snapshot(2), approved=False)
        with self.assertRaises(DomainError):
            service.publish_base_snapshot(snapshot)

    def test_merged_view_applies_only_approved_overrides(self):
        self.service.submit_override(
            "OV-5", "SG", ChangeType.TERMINOLOGY, "term:薪资谈判", "薪酬商谈"
        )
        pending_view = self.service.merged_view("SG")
        self.assertEqual(
            pending_view.items[0].payload, "薪资谈判"  # 未定稿前仍是基础内容
        )
        approve_and_finalize(
            self.service,
            "OV-5",
            [
                (ApproverRole.LANGUAGE_EXPERT, "语言专家甲"),
                (ApproverRole.SUBJECT_LEAD, "学科负责人乙"),
            ],
        )
        view = self.service.merged_view("SG")
        merged = {item.key: item for item in view.items}
        self.assertEqual(merged["term:薪资谈判"].payload, "薪酬商谈")
        self.assertEqual(merged["term:薪资谈判"].source, "OV-5")
        self.assertEqual(merged["case:职场对话"].source, "base")


if __name__ == "__main__":
    unittest.main()
