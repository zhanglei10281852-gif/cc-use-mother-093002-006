import unittest

from helpers import approve_and_finalize, base_snapshot, make_service
from locale_release import (
    ApprovalIncomplete,
    ApproverRole,
    ChangeType,
    ContentItem,
    OverrideStatus,
)


class ApprovalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        self.service.open_branch("SG")

    def _submit(self, override_id, change_type, target_key, payload="本地化内容"):
        return self.service.submit_override(
            override_id, "SG", change_type, target_key, payload
        )

    def test_terminology_requires_language_expert_and_subject_lead(self):
        self._submit("OV-T", ChangeType.TERMINOLOGY, "term:薪资谈判", "薪酬商谈")
        self.service.record_approval(
            "AP-1", "OV-T", ApproverRole.LANGUAGE_EXPERT, "语言专家甲"
        )
        with self.assertRaises(ApprovalIncomplete) as ctx:
            self.service.finalize_override("OV-T")
        codes = [issue.code for issue in ctx.exception.evaluation.issues]
        self.assertEqual(codes, ["missing_role"])

        self.service.record_approval(
            "AP-2", "OV-T", ApproverRole.SUBJECT_LEAD, "学科负责人乙"
        )
        finalized = self.service.finalize_override("OV-T")
        self.assertEqual(finalized.status, OverrideStatus.APPROVED)

    def test_schedule_requires_only_partner_school(self):
        self._submit("OV-S", ChangeType.SCHEDULE, "lesson:3", "40分钟")
        with self.assertRaises(ApprovalIncomplete):
            self.service.finalize_override("OV-S")
        approve_and_finalize(
            self.service, "OV-S", [(ApproverRole.PARTNER_SCHOOL, "合作院校丙")]
        )

    def test_case_requires_subject_lead_and_partner_school(self):
        self._submit("OV-C", ChangeType.CASE, "case:职场对话", "本地案例")
        self.service.record_approval(
            "AP-3", "OV-C", ApproverRole.SUBJECT_LEAD, "学科负责人乙"
        )
        with self.assertRaises(ApprovalIncomplete):
            self.service.finalize_override("OV-C")
        self.service.record_approval(
            "AP-4", "OV-C", ApproverRole.PARTNER_SCHOOL, "合作院校丙"
        )
        self.assertEqual(
            self.service.finalize_override("OV-C").status, OverrideStatus.APPROVED
        )

    def test_expired_confirmation_is_rejected(self):
        self._submit("OV-E", ChangeType.SCHEDULE, "lesson:3", "40分钟")
        self.service.record_approval(
            "AP-5", "OV-E", ApproverRole.PARTNER_SCHOOL, "合作院校丙"
        )
        self.clock.advance(days=31)
        with self.assertRaises(ApprovalIncomplete) as ctx:
            self.service.finalize_override("OV-E")
        codes = [issue.code for issue in ctx.exception.evaluation.issues]
        self.assertIn("expired", codes)

    def test_confirmation_stales_after_override_edit(self):
        self._submit("OV-R", ChangeType.SCHEDULE, "lesson:3", "40分钟")
        approve_and_finalize(
            self.service, "OV-R", [(ApproverRole.PARTNER_SCHOOL, "合作院校丙")]
        )
        # 上游修订了被覆盖的内容键，产生冲突
        self.service.publish_base_snapshot(
            base_snapshot(
                2,
                items=(
                    ContentItem("term:薪资谈判", ChangeType.TERMINOLOGY, "薪资谈判"),
                    ContentItem("case:职场对话", ChangeType.CASE, "总部案例文本"),
                    ContentItem("lesson:3", ChangeType.SCHEDULE, "50分钟"),
                ),
            )
        )
        self.assertEqual(
            self.service.repo.branches["SG"].overrides["OV-R"].status,
            OverrideStatus.CONFLICTED,
        )
        resolved = self.service.resolve_conflict("OV-R", "edit", new_payload="30分钟")
        self.assertEqual(resolved.revision, 2)
        evaluation = self.service.evaluate_override("OV-R")
        codes = [issue.code for issue in evaluation.issues]
        self.assertIn("stale_override_revision", codes)


if __name__ == "__main__":
    unittest.main()
