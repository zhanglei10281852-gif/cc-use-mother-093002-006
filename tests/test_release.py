import unittest

from helpers import approve_and_finalize, base_snapshot, make_service
from locale_release import (
    ApproverRole,
    ChangeType,
    ContentItem,
    DependencyPin,
    DomainError,
    LocalizationService,
    ReleaseBlocked,
    ReleaseTaskState,
)


def _prepare_two_regions(service):
    service.open_branch("SG")
    service.open_branch("TH")
    service.submit_override(
        "OV-SG", "SG", ChangeType.TERMINOLOGY, "term:薪资谈判", "薪酬商谈"
    )
    approve_and_finalize(
        service,
        "OV-SG",
        [
            (ApproverRole.LANGUAGE_EXPERT, "语言专家甲"),
            (ApproverRole.SUBJECT_LEAD, "学科负责人乙"),
        ],
    )
    service.submit_override("OV-TH", "TH", ChangeType.SCHEDULE, "lesson:3", "40分钟")
    approve_and_finalize(
        service, "OV-TH", [(ApproverRole.PARTNER_SCHOOL, "合作院校丙")]
    )


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        _prepare_two_regions(self.service)

    def test_batch_locks_digest_and_dependency_versions(self):
        batch = self.service.create_release_batch("B-1", ["SG", "TH"])
        self.assertEqual(batch.base_revision, 1)
        self.assertEqual(batch.base_digest, self.service._current_base().digest())
        sg_task = batch.tasks["SG"]
        self.assertEqual(sg_task.content_digest, self.service.merged_view("SG").digest)
        self.assertEqual(sg_task.dependency_pins, (DependencyPin("OV-SG", 1),))
        th_task = batch.tasks["TH"]
        self.assertEqual(th_task.dependency_pins, (DependencyPin("OV-TH", 1),))

    def test_unresolved_conflict_blocks_release(self):
        self.service.publish_base_snapshot(
            base_snapshot(
                2,
                items=(
                    ContentItem("term:薪资谈判", ChangeType.TERMINOLOGY, "薪酬谈判"),
                    ContentItem("case:职场对话", ChangeType.CASE, "总部案例文本"),
                    ContentItem("lesson:3", ChangeType.SCHEDULE, "45分钟"),
                ),
            )
        )
        with self.assertRaises(ReleaseBlocked) as ctx:
            self.service.create_release_batch("B-2", ["SG"])
        codes = [r.code for r in ctx.exception.reasons]
        self.assertIn("unresolved_conflict", codes)

    def test_expired_confirmation_blocks_release(self):
        self.clock.advance(days=31)
        with self.assertRaises(ReleaseBlocked) as ctx:
            self.service.create_release_batch("B-3", ["SG"])
        codes = [r.code for r in ctx.exception.reasons]
        self.assertIn("expired_confirmation", codes)

    def test_missing_approval_blocks_release(self):
        self.service.open_branch("VN")
        self.service.submit_override(
            "OV-VN", "VN", ChangeType.SCHEDULE, "lesson:3", "50分钟"
        )
        with self.assertRaises(ReleaseBlocked) as ctx:
            self.service.create_release_batch("B-4", ["VN"])
        codes = [r.code for r in ctx.exception.reasons]
        self.assertIn("missing_approval", codes)


class ReleaseExecutionTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        _prepare_two_regions(self.service)
        self.batch = self.service.create_release_batch("B-1", ["SG", "TH"])

    def test_region_failure_is_isolated_and_retryable(self):
        def flaky(region, task):
            if region == "TH":
                raise RuntimeError("交付通道超时")

        self.service.execute_batch("B-1", deliver=flaky)
        sg_task = self.batch.tasks["SG"]
        th_task = self.batch.tasks["TH"]
        self.assertEqual(sg_task.state, ReleaseTaskState.SUCCEEDED)
        self.assertEqual(th_task.state, ReleaseTaskState.FAILED)
        self.assertEqual(th_task.last_error, "交付通道超时")
        self.assertFalse(self.batch.completed)
        # 失败地区未留下已发布版本，成功地区已发布
        self.assertIsNotNone(self.service.active_release("SG"))
        self.assertIsNone(self.service.active_release("TH"))
        # 阻断原因可见
        codes = [r.code for r in self.service.blocking_reasons("TH")]
        self.assertIn("release_failed", codes)

        # 隔离重试只影响失败地区
        self.service.retry_region("B-1", "TH")
        self.assertEqual(th_task.state, ReleaseTaskState.SUCCEEDED)
        self.assertEqual(th_task.attempts, 2)
        self.assertEqual(sg_task.attempts, 1)
        self.assertTrue(self.batch.completed)
        self.assertIsNotNone(self.service.active_release("TH"))

    def test_retry_rejects_non_failed_task(self):
        self.service.execute_batch("B-1")
        with self.assertRaises(DomainError):
            self.service.retry_region("B-1", "SG")

    def test_restarted_process_resumes_from_checkpoints(self):
        def flaky(region, task):
            if region == "TH":
                raise RuntimeError("交付通道超时")

        self.service.execute_batch("B-1", deliver=flaky)

        # 模拟进程重启：新服务实例接管同一仓储
        restarted = LocalizationService(repo=self.service.repo, clock=self.clock)
        position = restarted.resume_position("B-1")
        self.assertEqual(position.phase, "publish")
        self.assertEqual(position.next_action, "execute_regions")
        self.assertEqual(position.pending_regions, ("TH",))
        self.assertEqual(position.last_checkpoint.event, "task_failed")

        restarted.resume_batch("B-1")
        self.assertTrue(self.batch.completed)
        self.assertEqual(self.batch.tasks["TH"].attempts, 2)
        position = restarted.resume_position("B-1")
        self.assertEqual(position.phase, "completed")
        self.assertEqual(position.last_checkpoint.event, "batch_completed")

    def test_checkpoints_form_append_only_trail(self):
        self.service.execute_batch("B-1")
        events = [c.event for c in self.service.repo.checkpoints]
        self.assertEqual(
            events,
            [
                "batch_created",
                "task_started",
                "task_succeeded",
                "task_started",
                "task_succeeded",
                "batch_completed",
            ],
        )
        seqs = [c.seq for c in self.service.repo.checkpoints]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


if __name__ == "__main__":
    unittest.main()
