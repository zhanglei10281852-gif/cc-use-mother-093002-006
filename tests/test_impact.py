import unittest

from helpers import approve_and_finalize, base_snapshot, make_service
from locale_release import (
    ApproverRole,
    ChangeType,
    ContentItem,
    ImpactKind,
    OverrideStatus,
)

V2_ITEMS = (
    ContentItem("term:薪资谈判", ChangeType.TERMINOLOGY, "薪酬谈判"),  # 被修订
    ContentItem("case:职场对话", ChangeType.CASE, "总部案例文本"),
    ContentItem("lesson:3", ChangeType.SCHEDULE, "45分钟"),
)


class ImpactAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        self.service.open_branch("SG")
        self.service.open_branch("TH")
        self.service.submit_override(
            "OV-SG", "SG", ChangeType.TERMINOLOGY, "term:薪资谈判", "薪酬商谈"
        )
        approve_and_finalize(
            self.service,
            "OV-SG",
            [
                (ApproverRole.LANGUAGE_EXPERT, "语言专家甲"),
                (ApproverRole.SUBJECT_LEAD, "学科负责人乙"),
            ],
        )
        self.service.submit_override(
            "OV-TH", "TH", ChangeType.CASE, "case:职场对话", "泰国本地案例"
        )
        approve_and_finalize(
            self.service,
            "OV-TH",
            [
                (ApproverRole.SUBJECT_LEAD, "学科负责人乙"),
                (ApproverRole.PARTNER_SCHOOL, "合作院校丙"),
            ],
        )

    def test_upstream_change_flags_conflict_and_stale_confirmation(self):
        reports = self.service.publish_base_snapshot(base_snapshot(2, items=V2_ITEMS))

        sg_report = reports["SG"]
        self.assertEqual(sg_report.from_revision, 1)
        self.assertEqual(sg_report.to_revision, 2)
        self.assertEqual(sg_report.conflicted_override_ids, ("OV-SG",))
        self.assertEqual(
            sg_report.impacts[0].kind, ImpactKind.CONTENT_CONFLICT
        )
        branch_sg = self.service.repo.branches["SG"]
        self.assertEqual(
            branch_sg.overrides["OV-SG"].status, OverrideStatus.CONFLICTED
        )
        self.assertEqual(branch_sg.base_revision, 2)

        # TH 的覆盖项未被直接修订，但既有确认随基础版本推进而失效
        th_report = reports["TH"]
        self.assertEqual(len(th_report.impacts), 1)
        self.assertEqual(th_report.impacts[0].kind, ImpactKind.APPROVAL_STALE)
        branch_th = self.service.repo.branches["TH"]
        self.assertEqual(
            branch_th.overrides["OV-TH"].status, OverrideStatus.PENDING_APPROVAL
        )
        evaluation = self.service.evaluate_override("OV-TH")
        codes = [issue.code for issue in evaluation.issues]
        self.assertIn("stale_base_revision", codes)

    def test_reports_are_stored_for_status_queries(self):
        self.service.publish_base_snapshot(base_snapshot(2, items=V2_ITEMS))
        self.assertEqual(len(self.service.repo.impact_reports["SG"]), 1)
        self.assertEqual(len(self.service.repo.impact_reports["TH"]), 1)

    def test_resolve_conflict_by_edit_requires_fresh_approvals(self):
        self.service.publish_base_snapshot(base_snapshot(2, items=V2_ITEMS))
        resolved = self.service.resolve_conflict("OV-SG", "edit", new_payload="薪酬协商")
        self.assertEqual(resolved.status, OverrideStatus.PENDING_APPROVAL)
        self.assertEqual(resolved.revision, 2)
        self.assertEqual(resolved.base_revision, 2)
        approve_and_finalize(
            self.service,
            "OV-SG",
            [
                (ApproverRole.LANGUAGE_EXPERT, "语言专家甲"),
                (ApproverRole.SUBJECT_LEAD, "学科负责人乙"),
            ],
        )
        self.assertEqual(self.service.blocking_reasons("SG"), [])

    def test_resolve_conflict_by_accept_upstream(self):
        self.service.publish_base_snapshot(base_snapshot(2, items=V2_ITEMS))
        resolved = self.service.resolve_conflict("OV-SG", "accept_upstream")
        self.assertEqual(resolved.status, OverrideStatus.SUPERSEDED)
        view = self.service.merged_view("SG")
        merged = {item.key: item for item in view.items}
        self.assertEqual(merged["term:薪资谈判"].payload, "薪酬谈判")
        self.assertEqual(merged["term:薪资谈判"].source, "base")

    def test_resolve_conflict_by_revalidate_keeps_payload(self):
        self.service.publish_base_snapshot(base_snapshot(2, items=V2_ITEMS))
        resolved = self.service.resolve_conflict("OV-SG", "revalidate")
        self.assertEqual(resolved.payload, "薪酬商谈")
        self.assertEqual(resolved.base_revision, 2)
        self.assertEqual(resolved.status, OverrideStatus.PENDING_APPROVAL)
        # 旧确认基于 R1，需要按 R2 重新确认
        evaluation = self.service.evaluate_override("OV-SG")
        self.assertFalse(evaluation.satisfied)


if __name__ == "__main__":
    unittest.main()
