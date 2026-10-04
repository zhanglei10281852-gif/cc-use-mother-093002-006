import unittest

from helpers import approve_and_finalize, make_service
from locale_release import (
    ApproverRole,
    ChangeType,
    DomainError,
    ReleaseBlocked,
    RollbackState,
)


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        self.service.open_branch("SG")
        self.service.open_branch("TH")
        # 第一次发布：SG 带课时调整
        self.service.submit_override(
            "OV-1", "SG", ChangeType.SCHEDULE, "lesson:3", "40分钟"
        )
        approve_and_finalize(
            self.service, "OV-1", [(ApproverRole.PARTNER_SCHOOL, "合作院校丙")]
        )
        self.service.create_release_batch("B-1", ["SG"])
        self.service.execute_batch("B-1")
        self.digest_v1 = self.service.active_release("SG").content_digest
        # 第二次发布：SG 再增加术语覆盖
        self.service.submit_override(
            "OV-2", "SG", ChangeType.TERMINOLOGY, "term:薪资谈判", "薪酬商谈"
        )
        approve_and_finalize(
            self.service,
            "OV-2",
            [
                (ApproverRole.LANGUAGE_EXPERT, "语言专家甲"),
                (ApproverRole.SUBJECT_LEAD, "学科负责人乙"),
            ],
        )
        self.service.create_release_batch("B-2", ["SG"])
        self.service.execute_batch("B-2")
        self.digest_v2 = self.service.active_release("SG").content_digest
        self.assertNotEqual(self.digest_v1, self.digest_v2)

    def test_rollback_returns_to_most_recent_compatible_version(self):
        record = self.service.emergency_rollback("RB-1", "SG")
        self.assertEqual(record.from_batch_id, "B-2")
        self.assertEqual(record.to_batch_id, "B-1")
        self.assertEqual(record.to_digest, self.digest_v1)
        self.assertEqual(record.state, RollbackState.AWAITING_SCHOOL_CONFIRMATION)
        # 当前生效版本回到 B-1
        active = self.service.active_release("SG")
        self.assertEqual(active.batch_id, "B-1")
        self.assertEqual(active.content_digest, self.digest_v1)

    def test_rollback_waits_for_school_confirmation(self):
        self.service.emergency_rollback("RB-1", "SG")
        codes = [r.code for r in self.service.blocking_reasons("SG")]
        self.assertIn("rollback_pending_confirmation", codes)
        # 确认前不能发布新批次
        with self.assertRaises(ReleaseBlocked):
            self.service.create_release_batch("B-3", ["SG"])

        record = self.service.confirm_rollback("RB-1", "合作院校丙")
        self.assertEqual(record.state, RollbackState.CONFIRMED)
        self.assertEqual(record.confirmed_by, "合作院校丙")
        codes = [r.code for r in self.service.blocking_reasons("SG")]
        self.assertNotIn("rollback_pending_confirmation", codes)

    def test_rollback_without_compatible_version_fails(self):
        # TH 只有一次发布，没有可回退的兼容版本
        self.service.submit_override(
            "OV-TH", "TH", ChangeType.SCHEDULE, "lesson:3", "50分钟"
        )
        approve_and_finalize(
            self.service, "OV-TH", [(ApproverRole.PARTNER_SCHOOL, "合作院校丁")]
        )
        self.service.create_release_batch("B-TH", ["TH"])
        self.service.execute_batch("B-TH")
        with self.assertRaises(DomainError):
            self.service.emergency_rollback("RB-2", "TH")

    def test_rollback_without_any_release_fails(self):
        with self.assertRaises(DomainError):
            self.service.emergency_rollback("RB-3", "TH")

    def test_rollback_resume_position_tracks_confirmation(self):
        self.service.emergency_rollback("RB-1", "SG")
        position = self.service.resume_position("RB-1")
        self.assertEqual(position.phase, "rollback")
        self.assertEqual(position.next_action, "await_school_confirmation")
        self.assertEqual(position.pending_regions, ("SG",))
        self.assertEqual(position.last_checkpoint.event, "rollback_initiated")

        self.service.confirm_rollback("RB-1", "合作院校丙")
        position = self.service.resume_position("RB-1")
        self.assertEqual(position.phase, "completed")
        self.assertEqual(position.last_checkpoint.event, "rollback_confirmed")


if __name__ == "__main__":
    unittest.main()
