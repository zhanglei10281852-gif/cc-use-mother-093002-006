"""区域课程本地化发布平台的应用服务。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .approvals import (
    DEFAULT_APPROVAL_TTL,
    Approval,
    ApprovalEvaluation,
    ApproverRole,
    evaluate_approvals,
)
from .contracts import LocaleOverride
from .impact import ImpactKind, ImpactReport, compute_branch_impact
from .models import (
    TERMINAL_OVERRIDE_STATES,
    BaseCourseSnapshot,
    ChangeType,
    MergedItem,
    MergedView,
    OverrideRequest,
    OverrideStatus,
    RegionBranch,
    merged_view_digest,
)
from .release import (
    Checkpoint,
    DependencyPin,
    PublishedRelease,
    ReleaseBatch,
    ReleaseTask,
    ReleaseTaskState,
    RollbackRecord,
    RollbackState,
)
from .status import (
    BlockingReason,
    KeyDifference,
    RegionInheritanceDiff,
    ResumePosition,
)

DeliverFunc = Callable[[str, ReleaseTask], None]


class DomainError(Exception):
    """领域规则被违反。"""


class ReleaseBlocked(DomainError):
    """发布门禁未通过，携带全部阻断原因。"""

    def __init__(self, reasons: list[BlockingReason]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("; ".join(f"{r.code}: {r.detail}" for r in self.reasons))


class ApprovalIncomplete(DomainError):
    """审批组合未满足或确认已失效。"""

    def __init__(self, evaluation: ApprovalEvaluation) -> None:
        self.evaluation = evaluation
        super().__init__("; ".join(i.detail for i in evaluation.issues))


@dataclass
class Repository:
    """平台状态集合：可整体移交给重启后的服务实例以续接进程。"""

    base_snapshots: dict[int, BaseCourseSnapshot] = field(default_factory=dict)
    current_base_revision: Optional[int] = None
    branches: dict[str, RegionBranch] = field(default_factory=dict)
    approvals: dict[str, list[Approval]] = field(default_factory=dict)
    batches: dict[str, ReleaseBatch] = field(default_factory=dict)
    published: dict[str, list[PublishedRelease]] = field(default_factory=dict)
    rollbacks: dict[str, RollbackRecord] = field(default_factory=dict)
    impact_reports: dict[str, list[ImpactReport]] = field(default_factory=dict)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    override_registry: dict[str, LocaleOverride] = field(default_factory=dict)


class LocalizationService:
    """区域课程本地化发布平台的核心服务。"""

    def __init__(
        self,
        repo: Optional[Repository] = None,
        clock: Optional[Callable[[], datetime]] = None,
        approval_ttl: timedelta = DEFAULT_APPROVAL_TTL,
    ) -> None:
        self.repo = repo or Repository()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.approval_ttl = approval_ttl

    # ------------------------------------------------------------------
    # 基础课程版本（继承源）
    # ------------------------------------------------------------------
    def publish_base_snapshot(self, snapshot: BaseCourseSnapshot) -> dict[str, ImpactReport]:
        """登记经过批准的基础版本；后续版本会推进各分支继承并计算影响。"""
        if not snapshot.approved:
            raise DomainError("基础课程版本必须经过批准才能作为继承源")
        revision = snapshot.version.revision
        if revision in self.repo.base_snapshots:
            raise DomainError(f"基础版本 R{revision} 已登记")
        if self.repo.current_base_revision is None:
            self.repo.base_snapshots[revision] = snapshot
            self.repo.current_base_revision = revision
            return {}
        old = self.repo.base_snapshots[self.repo.current_base_revision]
        if revision <= old.version.revision:
            raise DomainError("基础版本只能向前推进")
        self.repo.base_snapshots[revision] = snapshot
        self.repo.current_base_revision = revision
        with_approvals = {oid for oid, aps in self.repo.approvals.items() if aps}
        reports: dict[str, ImpactReport] = {}
        for region, branch in self.repo.branches.items():
            report = compute_branch_impact(branch, old, snapshot, with_approvals)
            branch.base_revision = revision
            for impact in report.impacts:
                ov = branch.overrides[impact.override_id]
                if impact.kind == ImpactKind.CONTENT_CONFLICT:
                    branch.overrides[ov.override_id] = replace(
                        ov, status=OverrideStatus.CONFLICTED
                    )
                elif (
                    impact.kind == ImpactKind.APPROVAL_STALE
                    and ov.status == OverrideStatus.APPROVED
                ):
                    # 继承源变化后，既有确认不再有效，覆盖项回到待审批
                    branch.overrides[ov.override_id] = replace(
                        ov, status=OverrideStatus.PENDING_APPROVAL
                    )
            reports[region] = report
            self.repo.impact_reports.setdefault(region, []).append(report)
        return reports

    # ------------------------------------------------------------------
    # 地区分支与覆盖项
    # ------------------------------------------------------------------
    def open_branch(self, region: str) -> RegionBranch:
        if self.repo.current_base_revision is None:
            raise DomainError("尚未登记基础课程版本")
        if region in self.repo.branches:
            raise DomainError(f"地区分支 {region} 已存在")
        branch = RegionBranch(region, self.repo.current_base_revision)
        self.repo.branches[region] = branch
        return branch

    def submit_override(
        self,
        override_id: str,
        region: str,
        change_type: ChangeType,
        target_key: str,
        payload: str,
        note: str = "",
    ) -> OverrideRequest:
        branch = self._branch(region)
        snapshot = self._current_base()
        if target_key not in snapshot.item_map():
            raise DomainError(
                f"目标内容键 {target_key} 在基础版本 "
                f"R{snapshot.version.revision} 中不存在"
            )
        for existing in branch.active_overrides():
            if existing.target_key == target_key:
                raise DomainError(
                    f"内容键 {target_key} 已存在进行中的覆盖项 {existing.override_id}"
                )
        if override_id in self.repo.override_registry:
            raise DomainError(f"覆盖项 {override_id} 已登记")
        override = OverrideRequest(
            override_id=override_id,
            region=region,
            change_type=change_type,
            target_key=target_key,
            payload=payload,
            base_revision=branch.base_revision,
            note=note,
            status=OverrideStatus.PENDING_APPROVAL,
        )
        branch.overrides[override_id] = override
        self.repo.override_registry[override_id] = LocaleOverride(
            override_id, snapshot.version.entity_id, change_type.value
        )
        return override

    # ------------------------------------------------------------------
    # 审批
    # ------------------------------------------------------------------
    def record_approval(
        self, approval_id: str, override_id: str, role: ApproverRole, approver: str
    ) -> Approval:
        override = self._override(override_id)
        if override.status != OverrideStatus.PENDING_APPROVAL:
            raise DomainError("仅待审批状态的覆盖项可登记审批")
        branch = self.repo.branches[override.region]
        approval = Approval(
            approval_id=approval_id,
            override_id=override_id,
            role=role,
            approver=approver,
            approved_at=self.clock(),
            override_revision=override.revision,
            base_revision=branch.base_revision,
        )
        self.repo.approvals.setdefault(override_id, []).append(approval)
        return approval

    def evaluate_override(self, override_id: str) -> ApprovalEvaluation:
        override = self._override(override_id)
        branch = self.repo.branches[override.region]
        return evaluate_approvals(
            override,
            self.repo.approvals.get(override_id, []),
            branch.base_revision,
            self.clock(),
            self.approval_ttl,
        )

    def finalize_override(self, override_id: str) -> OverrideRequest:
        override = self._override(override_id)
        if override.status != OverrideStatus.PENDING_APPROVAL:
            raise DomainError("仅待审批状态的覆盖项可完成定稿")
        evaluation = self.evaluate_override(override_id)
        if not evaluation.satisfied:
            raise ApprovalIncomplete(evaluation)
        branch = self.repo.branches[override.region]
        updated = replace(override, status=OverrideStatus.APPROVED)
        branch.overrides[override_id] = updated
        return updated

    # ------------------------------------------------------------------
    # 冲突解决
    # ------------------------------------------------------------------
    def resolve_conflict(
        self, override_id: str, strategy: str, new_payload: Optional[str] = None
    ) -> OverrideRequest:
        """解决上游修订引入的冲突：edit 编辑、revalidate 确认沿用、accept_upstream 放弃。"""
        override = self._override(override_id)
        if override.status != OverrideStatus.CONFLICTED:
            raise DomainError("仅冲突状态的覆盖项需要解决")
        branch = self.repo.branches[override.region]
        if strategy == "edit":
            if not new_payload:
                raise DomainError("编辑解决需要提供新的覆盖内容")
            updated = replace(
                override,
                payload=new_payload,
                revision=override.revision + 1,
                base_revision=branch.base_revision,
                status=OverrideStatus.PENDING_APPROVAL,
            )
        elif strategy == "revalidate":
            updated = replace(
                override,
                base_revision=branch.base_revision,
                status=OverrideStatus.PENDING_APPROVAL,
            )
        elif strategy == "accept_upstream":
            updated = replace(override, status=OverrideStatus.SUPERSEDED)
        else:
            raise DomainError(f"未知的冲突解决策略 {strategy}")
        branch.overrides[override_id] = updated
        return updated

    # ------------------------------------------------------------------
    # 合并视图
    # ------------------------------------------------------------------
    def merged_view(self, region: str) -> MergedView:
        branch = self._branch(region)
        snapshot = self.repo.base_snapshots[branch.base_revision]
        approved = {
            ov.target_key: ov
            for ov in branch.overrides.values()
            if ov.status == OverrideStatus.APPROVED
        }
        items: list[MergedItem] = []
        for key, item in snapshot.item_map().items():
            override = approved.get(key)
            if override is None:
                items.append(MergedItem(key, item.change_type, item.payload, "base"))
            else:
                items.append(
                    MergedItem(key, override.change_type, override.payload, override.override_id)
                )
        return MergedView(
            region, branch.base_revision, tuple(items), merged_view_digest(items)
        )

    # ------------------------------------------------------------------
    # 发布批次
    # ------------------------------------------------------------------
    def create_release_batch(self, batch_id: str, regions: list[str]) -> ReleaseBatch:
        """创建发布批次：先过门禁，再锁定各地区内容摘要与依赖版本。"""
        if batch_id in self.repo.batches:
            raise DomainError(f"发布批次 {batch_id} 已存在")
        if not regions:
            raise DomainError("发布批次至少包含一个地区")
        reasons: list[BlockingReason] = []
        for region in regions:
            reasons.extend(self._gate_reasons(self._branch(region)))
        if reasons:
            raise ReleaseBlocked(reasons)
        base = self._current_base()
        tasks: dict[str, ReleaseTask] = {}
        for region in regions:
            view = self.merged_view(region)
            branch = self.repo.branches[region]
            pins = tuple(
                sorted(
                    (
                        DependencyPin(ov.override_id, ov.revision)
                        for ov in branch.overrides.values()
                        if ov.status == OverrideStatus.APPROVED
                    ),
                    key=lambda p: p.override_id,
                )
            )
            tasks[region] = ReleaseTask(region, view.digest, pins)
        batch = ReleaseBatch(batch_id, base.version.revision, base.digest(), self.clock(), tasks)
        self.repo.batches[batch_id] = batch
        self._checkpoint(
            batch_id, "publish", "batch_created", "-", f"地区: {', '.join(sorted(regions))}"
        )
        return batch

    def execute_batch(self, batch_id: str, deliver: Optional[DeliverFunc] = None) -> ReleaseBatch:
        """执行批次中所有待办地区任务，单个地区失败不影响其他任务。"""
        batch = self._batch(batch_id)
        for task in batch.tasks.values():
            if task.state == ReleaseTaskState.PENDING:
                self._run_task(batch, task, deliver)
        self._complete_if_done(batch)
        return batch

    def retry_region(
        self, batch_id: str, region: str, deliver: Optional[DeliverFunc] = None
    ) -> ReleaseTask:
        """隔离重试某个失败的地区任务，其他任务不受影响。"""
        batch = self._batch(batch_id)
        task = batch.tasks.get(region)
        if task is None:
            raise DomainError(f"批次 {batch_id} 中不存在地区 {region}")
        if task.state != ReleaseTaskState.FAILED:
            raise DomainError("仅失败的地区任务可隔离重试")
        task.state = ReleaseTaskState.PENDING
        self._run_task(batch, task, deliver)
        self._complete_if_done(batch)
        return task

    def resume_batch(self, batch_id: str, deliver: Optional[DeliverFunc] = None) -> ReleaseBatch:
        """重启后的进程从上次位置续接：重跑所有待办与失败任务。"""
        batch = self._batch(batch_id)
        for task in batch.tasks.values():
            if task.state == ReleaseTaskState.FAILED:
                task.state = ReleaseTaskState.PENDING
            if task.state == ReleaseTaskState.PENDING:
                self._run_task(batch, task, deliver)
        self._complete_if_done(batch)
        return batch

    def _run_task(
        self, batch: ReleaseBatch, task: ReleaseTask, deliver: Optional[DeliverFunc]
    ) -> None:
        task.state = ReleaseTaskState.RUNNING
        task.attempts += 1
        self._checkpoint(
            batch.batch_id, "publish", "task_started", task.region, f"第 {task.attempts} 次尝试"
        )
        try:
            (deliver or (lambda region, task: None))(task.region, task)
        except Exception as exc:  # 隔离失败：记录并继续其他地区
            task.state = ReleaseTaskState.FAILED
            task.last_error = str(exc)
            self._checkpoint(batch.batch_id, "publish", "task_failed", task.region, str(exc))
            return
        task.state = ReleaseTaskState.SUCCEEDED
        task.last_error = None
        self.repo.published.setdefault(task.region, []).append(
            PublishedRelease(
                region=task.region,
                batch_id=batch.batch_id,
                content_digest=task.content_digest,
                base_revision=batch.base_revision,
                dependency_pins=task.dependency_pins,
                published_at=self.clock(),
            )
        )
        self._checkpoint(
            batch.batch_id, "publish", "task_succeeded", task.region, task.content_digest
        )

    def _complete_if_done(self, batch: ReleaseBatch) -> None:
        if batch.completed:
            self._checkpoint(
                batch.batch_id, "publish", "batch_completed", "-", "全部地区发布成功"
            )

    # ------------------------------------------------------------------
    # 紧急撤回
    # ------------------------------------------------------------------
    def active_release(self, region: str) -> Optional[PublishedRelease]:
        history = [r for r in self.repo.published.get(region, []) if not r.withdrawn]
        return history[-1] if history else None

    def emergency_rollback(self, rollback_id: str, region: str) -> RollbackRecord:
        """紧急撤回：回到最近兼容版本，并进入等待学校确认状态。"""
        if rollback_id in self.repo.rollbacks:
            raise DomainError(f"撤回记录 {rollback_id} 已存在")
        branch = self._branch(region)
        active = self.active_release(region)
        if active is None:
            raise DomainError(f"地区 {region} 尚无已发布版本，无法撤回")
        candidates = [
            r
            for r in self.repo.published.get(region, [])
            if not r.withdrawn
            and r.batch_id != active.batch_id
            and r.base_revision <= branch.base_revision
        ]
        if not candidates:
            raise DomainError(f"地区 {region} 没有可回退的兼容版本")
        target = candidates[-1]
        active.withdrawn = True
        record = RollbackRecord(
            rollback_id=rollback_id,
            region=region,
            from_batch_id=active.batch_id,
            to_batch_id=target.batch_id,
            to_digest=target.content_digest,
            state=RollbackState.AWAITING_SCHOOL_CONFIRMATION,
            initiated_at=self.clock(),
        )
        self.repo.rollbacks[rollback_id] = record
        self._checkpoint(
            rollback_id,
            "rollback",
            "rollback_initiated",
            region,
            f"从批次 {active.batch_id} 撤回到 {target.batch_id}",
        )
        return record

    def confirm_rollback(self, rollback_id: str, confirmer: str) -> RollbackRecord:
        record = self.repo.rollbacks.get(rollback_id)
        if record is None:
            raise DomainError(f"撤回记录 {rollback_id} 不存在")
        if record.state != RollbackState.AWAITING_SCHOOL_CONFIRMATION:
            raise DomainError("撤回已确认，无需重复操作")
        record.state = RollbackState.CONFIRMED
        record.confirmed_at = self.clock()
        record.confirmed_by = confirmer
        self._checkpoint(
            rollback_id, "rollback", "rollback_confirmed", record.region, f"确认人 {confirmer}"
        )
        return record

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def compare_regions(self, region_a: str, region_b: str) -> RegionInheritanceDiff:
        """直接比较两个地区的继承差异。"""
        branch_a = self._branch(region_a)
        branch_b = self._branch(region_b)

        def effective(branch: RegionBranch) -> dict[str, OverrideRequest]:
            return {
                ov.target_key: ov
                for ov in branch.overrides.values()
                if ov.status not in TERMINAL_OVERRIDE_STATES
            }

        view_a, view_b = effective(branch_a), effective(branch_b)
        only_a = tuple(sorted(set(view_a) - set(view_b)))
        only_b = tuple(sorted(set(view_b) - set(view_a)))
        differing = tuple(
            KeyDifference(
                key,
                view_a[key].status.value,
                view_b[key].status.value,
                view_a[key].payload,
                view_b[key].payload,
            )
            for key in sorted(set(view_a) & set(view_b))
            if (view_a[key].payload, view_a[key].status, view_a[key].change_type)
            != (view_b[key].payload, view_b[key].status, view_b[key].change_type)
        )
        return RegionInheritanceDiff(
            region_a,
            region_b,
            branch_a.base_revision,
            branch_b.base_revision,
            only_a,
            only_b,
            differing,
        )

    def blocking_reasons(self, region: str) -> list[BlockingReason]:
        """汇总某地区当前的全部阻断原因。"""
        branch = self._branch(region)
        reasons = self._gate_reasons(branch)
        for batch in self.repo.batches.values():
            task = batch.tasks.get(region)
            if task is not None and task.state == ReleaseTaskState.FAILED:
                reasons.append(
                    BlockingReason(
                        "release_failed",
                        f"批次 {batch.batch_id} 中地区任务失败: {task.last_error}",
                    )
                )
        return reasons

    def resume_position(self, process_id: str) -> ResumePosition:
        """说明重启的发布/撤回进程应续接的位置。"""
        if process_id in self.repo.batches:
            batch = self.repo.batches[process_id]
            pending = tuple(
                sorted(
                    region
                    for region, task in batch.tasks.items()
                    if task.state
                    in (ReleaseTaskState.PENDING, ReleaseTaskState.FAILED)
                )
            )
            last = self._last_checkpoint(process_id)
            if pending:
                return ResumePosition(
                    process_id, "publish", "execute_regions", pending, last
                )
            return ResumePosition(process_id, "completed", "none", (), last)
        if process_id in self.repo.rollbacks:
            record = self.repo.rollbacks[process_id]
            last = self._last_checkpoint(process_id)
            if record.state == RollbackState.AWAITING_SCHOOL_CONFIRMATION:
                return ResumePosition(
                    process_id,
                    "rollback",
                    "await_school_confirmation",
                    (record.region,),
                    last,
                )
            return ResumePosition(process_id, "completed", "none", (), last)
        raise DomainError(f"未知进程 {process_id}")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _gate_reasons(self, branch: RegionBranch) -> list[BlockingReason]:
        reasons: list[BlockingReason] = []
        if branch.base_revision != self.repo.current_base_revision:
            reasons.append(
                BlockingReason(
                    "base_revision_mismatch",
                    f"分支继承 R{branch.base_revision}，"
                    f"当前基础版本 R{self.repo.current_base_revision}",
                )
            )
        for ov in branch.overrides.values():
            if ov.status == OverrideStatus.CONFLICTED:
                reasons.append(
                    BlockingReason(
                        "unresolved_conflict", f"覆盖项 {ov.override_id} 存在未解决差异"
                    )
                )
            elif ov.status in (
                OverrideStatus.PENDING_APPROVAL,
                OverrideStatus.APPROVED,
            ):
                evaluation = self.evaluate_override(ov.override_id)
                for issue in evaluation.issues:
                    if issue.code == "missing_role":
                        code = "missing_approval"
                    elif issue.code == "expired":
                        code = "expired_confirmation"
                    else:
                        code = "stale_confirmation"
                    reasons.append(
                        BlockingReason(code, f"覆盖项 {ov.override_id}: {issue.detail}")
                    )
        for record in self.repo.rollbacks.values():
            if (
                record.region == branch.region
                and record.state == RollbackState.AWAITING_SCHOOL_CONFIRMATION
            ):
                reasons.append(
                    BlockingReason(
                        "rollback_pending_confirmation",
                        f"撤回 {record.rollback_id} 等待学校确认",
                    )
                )
        return reasons

    def _checkpoint(
        self, process_id: str, phase: str, event: str, region: str, detail: str = ""
    ) -> Checkpoint:
        entry = Checkpoint(
            seq=len(self.repo.checkpoints) + 1,
            process_id=process_id,
            phase=phase,
            event=event,
            region=region,
            at=self.clock(),
            detail=detail,
        )
        self.repo.checkpoints.append(entry)
        return entry

    def _last_checkpoint(self, process_id: str) -> Optional[Checkpoint]:
        entries = [c for c in self.repo.checkpoints if c.process_id == process_id]
        return entries[-1] if entries else None

    def _branch(self, region: str) -> RegionBranch:
        branch = self.repo.branches.get(region)
        if branch is None:
            raise DomainError(f"地区分支 {region} 不存在")
        return branch

    def _override(self, override_id: str) -> OverrideRequest:
        for branch in self.repo.branches.values():
            if override_id in branch.overrides:
                return branch.overrides[override_id]
        raise DomainError(f"覆盖项 {override_id} 不存在")

    def _batch(self, batch_id: str) -> ReleaseBatch:
        batch = self.repo.batches.get(batch_id)
        if batch is None:
            raise DomainError(f"发布批次 {batch_id} 不存在")
        return batch

    def _current_base(self) -> BaseCourseSnapshot:
        if self.repo.current_base_revision is None:
            raise DomainError("尚未登记基础课程版本")
        return self.repo.base_snapshots[self.repo.current_base_revision]
