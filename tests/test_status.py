import unittest

from helpers import approve_and_finalize, make_service
from locale_release import (
    ApproverRole,
    ChangeType,
    DomainError,
)


class StatusQueryTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        self.service.open_branch("SG")
        self.service.open_branch("TH")

    def test_compare_regions_shows_inheritance_diff(self):
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
        diff = self.service.compare_regions("SG", "TH")
        self.assertEqual(diff.base_revision_a, 1)
        self.assertEqual(diff.base_revision_b, 1)
        self.assertEqual(diff.only_in_a, ("term:薪资谈判",))
        self.assertEqual(diff.only_in_b, ())
        self.assertEqual(diff.differing, ())
        self.assertFalse(diff.identical)

    def test_compare_regions_detects_diverged_payloads(self):
        self.service.submit_override(
            "OV-SG", "SG", ChangeType.SCHEDULE, "lesson:3", "40分钟"
        )
        approve_and_finalize(
            self.service, "OV-SG", [(ApproverRole.PARTNER_SCHOOL, "合作院校丙")]
        )
        self.service.submit_override(
            "OV-TH", "TH", ChangeType.SCHEDULE, "lesson:3", "50分钟"
        )
        approve_and_finalize(
            self.service, "OV-TH", [(ApproverRole.PARTNER_SCHOOL, "合作院校丁")]
        )
        diff = self.service.compare_regions("SG", "TH")
        self.assertEqual(diff.only_in_a, ())
        self.assertEqual(diff.only_in_b, ())
        self.assertEqual(len(diff.differing), 1)
        entry = diff.differing[0]
        self.assertEqual(entry.target_key, "lesson:3")
        self.assertEqual(entry.payload_a, "40分钟")
        self.assertEqual(entry.payload_b, "50分钟")

    def test_identical_regions_report_no_diff(self):
        diff = self.service.compare_regions("SG", "TH")
        self.assertTrue(diff.identical)

    def test_blocking_reasons_empty_for_clean_branch(self):
        self.assertEqual(self.service.blocking_reasons("SG"), [])

    def test_resume_position_rejects_unknown_process(self):
        with self.assertRaises(DomainError):
            self.service.resume_position("NOPE")


if __name__ == "__main__":
    unittest.main()
