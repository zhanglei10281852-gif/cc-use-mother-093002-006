import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from locale_release import (
    ApproverRole,
    ChangeStatus,
    ChangeType,
    ContentSnapshot,
    ContentUnit,
    DependencyVersion,
    LocalizationChange,
    LocalizationPlatform,
    PublishStage,
    RollbackStage,
    TargetStatus,
)


def make_snapshot(revision: int, units: dict[str, str], deps: dict[str, str] | None = None) -> ContentSnapshot:
    return ContentSnapshot(
        entity_id="E-DEMO",
        revision=revision,
        units=tuple(ContentUnit(uid, f"单元{uid}", body) for uid, body in units.items()),
        dependencies=tuple(DependencyVersion(name, ver) for name, ver in (deps or {}).items()),
    )


class FixedClock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, delta: timedelta) -> None:
        self.t += delta


def fully_approve(platform: LocalizationPlatform, locale: str, change: LocalizationChange) -> None:
    from locale_release.approval import APPROVAL_RULES

    for role in sorted(APPROVAL_RULES[change.change_type]):
        platform.approve_change(locale, change.change_id, role, approver_id=f"{role}-{locale}")


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(datetime(2026, 9, 1, 9, 0))
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "state.json"
        self.platform = LocalizationPlatform(self.store, clock=self.clock)
        self.platform.register_base_snapshot(
            make_snapshot(
                1,
                {"U1": "职业术语：技术员", "U2": "案例：本地工厂", "U3": "课时：16周", "U4": "合规：通则"},
                {"media": "2.1.0", "glossary": "1.0.0"},
            )
        )
        self.platform.onboard_locale("TH")
        self.platform.onboard_locale("VN")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _prepare_thai_branch(self) -> None:
        # 泰国：术语覆盖（需语言专家+院校）与课时调整（需学科负责人+院校，避开当地校历）
        term = LocalizationChange("C-TH-1", ChangeType.TERMINOLOGY, "U1", "术语：技师（泰语区用法）", 1)
        sched = LocalizationChange("C-TH-2", ChangeType.SCHEDULE, "U3", "课时：避开四月宋干节，15周", 1)
        self.platform.submit_change("TH", term)
        self.platform.submit_change("TH", sched)
        fully_approve(self.platform, "TH", term)
        fully_approve(self.platform, "TH", sched)
        self.platform.confirm_school("TH", confirmed_by="泰国合作院校教务长")

    def _prepare_vietnam_branch(self) -> None:
        # 越南：同一术语给出不同含义的覆盖
        term = LocalizationChange("C-VN-1", ChangeType.TERMINOLOGY, "U1", "术语：技术员（越南语区用法）", 1)
        case = LocalizationChange("C-VN-2", ChangeType.CASE, "U2", "案例：河内园区实习", 1)
        self.platform.submit_change("VN", term)
        self.platform.submit_change("VN", case)
        fully_approve(self.platform, "VN", term)
        fully_approve(self.platform, "VN", case)
        self.platform.confirm_school("VN", confirmed_by="越南合作院校教务长")

    # ---- 审批组合 ----

    def test_partial_approval_does_not_publish(self) -> None:
        term = LocalizationChange("C-TH-1", ChangeType.TERMINOLOGY, "U1", "术语：技师", 1)
        self.platform.submit_change("TH", term)
        self.platform.approve_change("TH", "C-TH-1", ApproverRole.LANGUAGE_EXPERT, "lang-1")
        batch = self.platform.create_batch("B-1", ("TH",))
        self.platform.run_batch("B-1")
        status = self.platform.status("B-1", "TH")
        self.assertEqual(status["status"], TargetStatus.FAILED)
        self.assertTrue(any("合作院校" in b for b in status["blockers"]))

    def test_compliance_requires_all_three_roles(self) -> None:
        change = LocalizationChange("C-X", ChangeType.COMPLIANCE, "U4", "合规：越南专项声明", 1)
        self.platform.submit_change("VN", change)
        self.platform.approve_change("VN", "C-X", ApproverRole.LANGUAGE_EXPERT, "l")
        self.platform.approve_change("VN", "C-X", ApproverRole.SUBJECT_OWNER, "s")
        # 尚缺合作院校
        self.assertIn("合作院校", self.platform.approvals.missing_roles(self.platform.branches["VN"].change("C-X")))

    # ---- 正常发布：锁定摘要与依赖 ----

    def test_happy_path_locks_digest_and_dependencies(self) -> None:
        self._prepare_thai_branch()
        batch = self.platform.create_batch("B-1", ("TH",))
        self.platform.run_batch("B-1")
        status = self.platform.status("B-1", "TH")
        self.assertEqual(status["status"], TargetStatus.PUBLISHED)
        self.assertEqual(status["locked_dependencies"], {"media": "2.1.0", "glossary": "1.0.0"})
        self.assertTrue(status["locked_content_digest"])
        self.assertEqual(status["publish_stage"], PublishStage.DONE)

    # ---- 上游变更影响 ----

    def test_upstream_revision_marks_changes_stale_and_revokes_approvals(self) -> None:
        self._prepare_thai_branch()
        self.assertEqual(self.platform.branches["TH"].change("C-TH-1").status, ChangeStatus.APPROVED)
        # 总部修订：U1 术语条目改写
        new_snapshot = make_snapshot(
            2,
            {"U1": "职业术语：技师（总部修订）", "U2": "案例：本地工厂", "U3": "课时：16周", "U4": "合规：通则"},
            {"media": "2.1.0", "glossary": "1.0.0"},
        )
        affected = self.platform.promote_base(new_snapshot)
        self.assertEqual(affected, {"TH": ["C-TH-1"]})
        change = self.platform.branches["TH"].change("C-TH-1")
        self.assertEqual(change.status, ChangeStatus.STALE)
        # 分支仍停留在旧修订，待差异解决后显式继承
        self.assertEqual(self.platform.branches["TH"].base_revision, 1)
        self.assertEqual(self.platform.branches["TH"].pending_revision, 2)
        # 旧审批已作废，必须重新确认并重走审批
        self.assertEqual(self.platform.approvals.missing_roles(change), frozenset({
            ApproverRole.LANGUAGE_EXPERT, ApproverRole.PARTNER_SCHOOL
        }))

        # 未解决过期差异时不能发布
        batch = self.platform.create_batch("B-2", ("TH",))
        self.platform.run_batch("B-2")
        status = self.platform.status("B-2", "TH")
        self.assertEqual(status["status"], TargetStatus.FAILED)
        self.assertTrue(any("已过期" in b for b in status["blockers"]))

        # 地区确认覆盖仍适用 → 重新审批 → 继承新修订 → 院校重新确认 → 可发布
        self.platform.reconfirm_change("TH", "C-TH-1", "泰语用法在新术语上仍成立")
        fully_approve(self.platform, "TH", change)
        self.platform.inherit_upstream("TH")
        self.assertEqual(self.platform.branches["TH"].base_revision, 2)
        # 院校确认因内容 digest 变化而失效
        ok, reason = self.platform.approvals.school_confirmation_state(self.platform.branches["TH"])
        self.assertFalse(ok)
        self.platform.confirm_school("TH", "泰国合作院校教务长")
        self.platform.run_batch("B-2")
        self.assertEqual(self.platform.status("B-2", "TH")["status"], TargetStatus.PUBLISHED)

    def test_deleted_upstream_unit_creates_unresolved_conflict(self) -> None:
        self._prepare_thai_branch()
        new_snapshot = make_snapshot(
            2, {"U1": "x", "U2": "x", "U4": "x"}, {"media": "2.1.0", "glossary": "1.0.0"}
        )
        affected = self.platform.promote_base(new_snapshot)
        self.assertIn("C-TH-2", affected["TH"])
        self.assertEqual(
            self.platform.branches["TH"].change("C-TH-2").status,
            ChangeStatus.CONFLICTED,
        )

    def test_expired_school_confirmation_blocks_release(self) -> None:
        self._prepare_thai_branch()
        self.clock.advance(timedelta(days=31))  # 默认确认有效期 30 天
        batch = self.platform.create_batch("B-1", ("TH",))
        self.platform.run_batch("B-1")
        blockers = self.platform.status("B-1", "TH")["blockers"]
        self.assertTrue(any("过期" in b for b in blockers))

    # ---- 隔离与重试 ----

    def test_failed_locale_is_isolated_and_retried_without_affecting_others(self) -> None:
        self._prepare_thai_branch()
        self._prepare_vietnam_branch()
        batch = self.platform.create_batch("B-1", ("TH", "VN"))
        # 在泰国的提交阶段注入一次故障
        self.platform.engine.inject_fault("TH", PublishStage.COMMIT, "网关超时")
        self.platform.run_batch("B-1")

        th = self.platform.status("B-1", "TH")
        vn = self.platform.status("B-1", "VN")
        self.assertEqual(th["status"], TargetStatus.FAILED)
        self.assertEqual(vn["status"], TargetStatus.PUBLISHED)

        # 隔离泰国后批次可视为完成；泰国单独重试并成功
        self.platform.isolate_locale("B-1", "TH")
        self.assertEqual(self.platform.status("B-1", "TH")["status"], TargetStatus.ISOLATED)
        self.platform.retry_locale("B-1", "TH")
        self.assertEqual(self.platform.status("B-1", "TH")["status"], TargetStatus.PUBLISHED)
        # 重试从提交阶段重入：阻断检查与锁定不重复计数失败
        self.assertEqual(self.platform.status("B-1", "TH")["attempts"], 2)

    # ---- 紧急撤回 ----

    def test_emergency_recall_restores_latest_compatible_and_awaits_school(self) -> None:
        # 先发布修订1批次（旧依赖主版本），再发布修订2批次
        self._prepare_thai_branch()
        batch1 = self.platform.create_batch("B-OLD", ("TH",))
        self.platform.run_batch("B-OLD")

        new_snapshot = make_snapshot(
            2,
            {"U1": "职业术语：技术员（总部第二版）", "U2": "案例：本地工厂", "U3": "课时：16周", "U4": "合规：通则"},
            {"media": "2.1.0", "glossary": "1.0.0"},
        )
        affected = self.platform.promote_base(new_snapshot)
        for cid in affected["TH"]:
            self.platform.reconfirm_change("TH", cid)
            fully_approve(self.platform, "TH", self.platform.branches["TH"].change(cid))
        self.platform.inherit_upstream("TH")
        self.platform.confirm_school("TH", "泰国合作院校教务长")
        batch2 = self.platform.create_batch("B-NEW", ("TH",))
        self.platform.run_batch("B-NEW")
        self.assertEqual(self.platform.registry.current("TH").batch_id, "B-NEW")

        # 紧急撤回：回到最近兼容版本（B-OLD），并等待院校确认
        self.platform.emergency_recall("B-NEW")
        status = self.platform.status("B-NEW", "TH")
        self.assertEqual(status["status"], TargetStatus.ROLLED_BACK)
        self.assertEqual(self.platform.registry.current("TH").batch_id, "B-OLD")
        self.assertIn("批次 B-OLD / 修订 1", status["restored_to"])
        self.assertTrue(any("等待合作院校重新确认" in b for b in status["blockers"]))
        self.assertEqual(status["rollback_stage"], RollbackStage.DONE)

    def test_recall_with_no_compatible_version_fails_and_reports_blocker(self) -> None:
        self._prepare_thai_branch()
        batch = self.platform.create_batch("B-1", ("TH",))
        self.platform.run_batch("B-1")
        # 该批次是首次发布，没有更早的兼容版本
        self.platform.emergency_recall("B-1")
        status = self.platform.status("B-1", "TH")
        self.assertEqual(status["status"], TargetStatus.FAILED)
        self.assertTrue(any("没有可回退的最近兼容版本" in b for b in status["blockers"]))

    # ---- 两地继承差异对比 ----

    def test_compare_locales_reports_divergence(self) -> None:
        self._prepare_thai_branch()
        self._prepare_vietnam_branch()
        diff = self.platform.compare("TH", "VN")
        self.assertTrue(diff["based_on_same_revision"])
        # U1 术语两地都有覆盖但内容不同
        self.assertIn("术语覆盖→U1", diff["payload_diverged"])
        self.assertTrue(any("课时调整→U3" in x for x in diff["only_in_a"]))
        self.assertTrue(any("案例替换→U2" in x for x in diff["only_in_b"]))

    def test_compare_reports_different_base_revisions(self) -> None:
        self._prepare_thai_branch()
        self._prepare_vietnam_branch()
        new_snapshot = make_snapshot(
            2,
            {"U1": "职业术语：技术员v2", "U2": "案例：本地工厂", "U3": "课时：16周", "U4": "合规：通则"},
            {"media": "2.1.0", "glossary": "1.0.0"},
        )
        affected = self.platform.promote_base(new_snapshot)
        # 泰国完成差异解决与继承，越南仍停在旧修订（rev2 挂起）
        for cid in affected["TH"]:
            self.platform.reconfirm_change("TH", cid)
            fully_approve(self.platform, "TH", self.platform.branches["TH"].change(cid))
        self.platform.inherit_upstream("TH")
        diff = self.platform.compare("TH", "VN")
        self.assertFalse(diff["based_on_same_revision"])
        self.assertEqual((diff["base_revision_a"], diff["base_revision_b"]), (2, 1))
        self.assertIsNone(diff["pending_revision_a"])
        self.assertEqual(diff["pending_revision_b"], 2)
        self.assertFalse(diff["inheritance_in_sync"])

    # ---- 重启接续 ----

    def test_restarted_process_resumes_at_failed_stage_from_checkpoint(self) -> None:
        self._prepare_thai_branch()
        self._prepare_vietnam_branch()
        batch = self.platform.create_batch("B-1", ("TH", "VN"))
        self.platform.engine.inject_fault("TH", PublishStage.COMMIT, "提交超时")
        self.platform.run_batch("B-1")

        resume = self.platform.resume("B-1", "TH")
        self.assertEqual(resume["pipeline"], "发布")
        self.assertEqual(resume["last_completed_stage"], PublishStage.LOCK)
        self.assertEqual(resume["resume_from"], PublishStage.COMMIT)

        # 模拟进程重启：从磁盘重新载入平台，再推进批次
        restarted = LocalizationPlatform(self.store, clock=self.clock)
        self.assertEqual(restarted.status("B-1", "VN")["status"], TargetStatus.PUBLISHED)
        self.assertEqual(restarted.status("B-1", "TH")["status"], TargetStatus.FAILED)
        restarted.isolate_locale("B-1", "TH")
        restarted.retry_locale("B-1", "TH")
        self.assertEqual(restarted.status("B-1", "TH")["status"], TargetStatus.PUBLISHED)
        # 锁定的内容摘要在重启前后一致
        self.assertTrue(restarted.status("B-1", "TH")["locked_content_digest"])

    def test_restart_resumes_mid_rollback(self) -> None:
        self._prepare_thai_branch()
        old = self.platform.create_batch("B-OLD", ("TH",))
        self.platform.run_batch("B-OLD")
        new_snapshot = make_snapshot(
            2,
            {"U1": "职业术语：技术员（总部第二版）", "U2": "案例：本地工厂", "U3": "课时：16周", "U4": "合规：通则"},
            {"media": "2.1.0", "glossary": "1.0.0"},
        )
        affected = self.platform.promote_base(new_snapshot)
        for cid in affected.get("TH", ()):
            self.platform.reconfirm_change("TH", cid)
            fully_approve(self.platform, "TH", self.platform.branches["TH"].change(cid))
        self.platform.inherit_upstream("TH")
        self.platform.confirm_school("TH", "泰国合作院校教务长")
        self.platform.create_batch("B-NEW", ("TH",))
        self.platform.run_batch("B-NEW")

        # 在撤回的“恢复”阶段注入故障，重启后应从该阶段接续
        self.platform.engine.inject_fault("TH", RollbackStage.RESTORE, "回退通道不可用")
        self.platform.emergency_recall("B-NEW")
        self.assertEqual(self.platform.resume("B-NEW", "TH")["resume_from"], RollbackStage.RESTORE)

        restarted = LocalizationPlatform(self.store, clock=self.clock)
        restarted.run_batch("B-NEW")
        status = restarted.status("B-NEW", "TH")
        self.assertEqual(status["status"], TargetStatus.ROLLED_BACK)
        self.assertEqual(restarted.registry.current("TH").batch_id, "B-OLD")


if __name__ == "__main__":
    unittest.main()
