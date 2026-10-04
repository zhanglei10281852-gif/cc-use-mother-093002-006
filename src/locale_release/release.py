"""发布批次：锁定、检查点、隔离重试与紧急撤回。

批次对每个地区独立推进一条小型阶段流水线，并在每个阶段完成后
写入检查点；进程重启后凭持久化状态从“原执行位置/原回滚位置”接续。
个别地区失败可隔离（不影响其他地区）后单独重试。
紧急撤回把已发布地区恢复到最近的兼容版本，再等待合作院校重新确认。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping

from .approval import ApprovalService
from .content import ContentSnapshot
from .localization import ChangeStatus, LocaleBranch


class TargetStatus:
    PENDING = "待发布"
    PUBLISHING = "发布中"
    PUBLISHED = "已发布"
    FAILED = "失败"
    ISOLATED = "已隔离"
    ROLLING_BACK = "撤回中"
    ROLLED_BACK = "已回退待院校确认"
    SKIPPED = "未参与撤回"  # 批次中从未发布成功、无需回退的地区


class BatchPhase:
    PUBLISH = "发布"
    ROLLBACK = "紧急撤回"
    COMPLETE = "完成"


class PublishStage:
    BLOCKED_CHECK = "阻断检查"
    LOCK = "锁定内容摘要与依赖版本"
    COMMIT = "提交发布"
    DONE = "发布完成"

    ORDER = (BLOCKED_CHECK, LOCK, COMMIT, DONE)


class RollbackStage:
    RECALL = "撤回当前版本"
    RESTORE = "恢复最近兼容版本"
    NOTIFY = "转入院校确认等待"
    DONE = "回滚完成"

    ORDER = (RECALL, RESTORE, NOTIFY, DONE)


@dataclass(frozen=True)
class ReleaseRecord:
    """一次成功发布留下的不可变记录，是紧急撤回时寻找兼容版本的依据。"""

    locale_code: str
    batch_id: str
    base_revision: int
    snapshot_digest: str
    content_digest: str
    dependencies: tuple[tuple[str, str], ...]
    published_at: datetime

    def dependency_map(self) -> dict[str, str]:
        return dict(self.dependencies)

    def is_compatible_with(self, dependencies: Mapping[str, str]) -> bool:
        """兼容判定：同名依赖的主版本一致即视为可回退的兼容版本。"""
        mine = self.dependency_map()
        if set(mine) != set(dependencies):
            return False
        return all(ver.split(".", 1)[0] == mine[name].split(".", 1)[0] for name, ver in dependencies.items())

    def to_dict(self) -> dict:
        return {
            "locale_code": self.locale_code,
            "batch_id": self.batch_id,
            "base_revision": self.base_revision,
            "snapshot_digest": self.snapshot_digest,
            "content_digest": self.content_digest,
            "dependencies": [list(pair) for pair in self.dependencies],
            "published_at": self.published_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "ReleaseRecord":
        return cls(
            locale_code=data["locale_code"],
            batch_id=data["batch_id"],
            base_revision=data["base_revision"],
            snapshot_digest=data["snapshot_digest"],
            content_digest=data["content_digest"],
            dependencies=tuple((p[0], p[1]) for p in data["dependencies"]),
            published_at=datetime.fromisoformat(data["published_at"]),
        )


class ReleaseRegistry:
    """各地区的发布历史与当前生效版本。"""

    def __init__(self) -> None:
        self._history: dict[str, list[ReleaseRecord]] = {}
        self._current: dict[str, ReleaseRecord] = {}

    def record_release(self, rec: ReleaseRecord) -> None:
        self._history.setdefault(rec.locale_code, []).append(rec)
        self._current[rec.locale_code] = rec

    def current(self, locale_code: str) -> ReleaseRecord | None:
        return self._current.get(locale_code)

    def latest_compatible(
        self,
        locale_code: str,
        dependencies: Mapping[str, str],
        exclude_batch_id: str | None = None,
    ) -> ReleaseRecord | None:
        """最近一个依赖兼容的历史版本（紧急撤回的回退目标）。"""
        candidates = [
            r
            for r in reversed(self._history.get(locale_code, ()))
            if r.is_compatible_with(dependencies) and r.batch_id != exclude_batch_id
        ]
        return candidates[0] if candidates else None

    def replace_current(self, locale_code: str, rec: ReleaseRecord | None) -> None:
        if rec is None:
            self._current.pop(locale_code, None)
        else:
            self._current[locale_code] = rec

    def to_dict(self) -> dict:
        return {
            "history": {k: [r.to_dict() for r in v] for k, v in self._history.items()},
            "current": {k: r.to_dict() for k, r in self._current.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "ReleaseRegistry":
        registry = cls()
        for locale, records in data.get("history", {}).items():
            registry._history[locale] = [ReleaseRecord.from_dict(r) for r in records]
        for locale, rec in data.get("current", {}).items():
            registry._current[locale] = ReleaseRecord.from_dict(rec)
        return registry


@dataclass
class LocaleTarget:
    """批次中单个地区的执行状态与检查点。"""

    locale_code: str
    status: str = TargetStatus.PENDING
    stage: str = ""
    rollback_stage: str = ""
    blockers: tuple[str, ...] = ()
    attempts: int = 0
    isolated: bool = False
    locked_content_digest: str = ""
    restored_to: str = ""  # 回退到的兼容版本批次/时间描述

    def to_dict(self) -> dict:
        return {
            "locale_code": self.locale_code,
            "status": self.status,
            "stage": self.stage,
            "rollback_stage": self.rollback_stage,
            "blockers": list(self.blockers),
            "attempts": self.attempts,
            "isolated": self.isolated,
            "locked_content_digest": self.locked_content_digest,
            "restored_to": self.restored_to,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "LocaleTarget":
        return cls(
            locale_code=data["locale_code"],
            status=data["status"],
            stage=data.get("stage", ""),
            rollback_stage=data.get("rollback_stage", ""),
            blockers=tuple(data.get("blockers", ())),
            attempts=data.get("attempts", 0),
            isolated=data.get("isolated", False),
            locked_content_digest=data.get("locked_content_digest", ""),
            restored_to=data.get("restored_to", ""),
        )


class InvalidReleaseState(Exception):
    """批次/地区状态不允许该操作。"""


@dataclass
class ReleaseBatch:
    batch_id: str
    base_entity_id: str
    base_revision: int
    snapshot_digest: str
    locked_dependencies: dict[str, str]
    targets: dict[str, LocaleTarget]
    phase: str = BatchPhase.PUBLISH
    created_at: datetime = field(default_factory=datetime.now)

    def target(self, locale_code: str) -> LocaleTarget:
        try:
            return self.targets[locale_code]
        except KeyError:
            raise KeyError(f"地区 {locale_code} 不在批次 {self.batch_id} 中") from None

    def is_finished(self) -> bool:
        return all(
            t.status
            in (TargetStatus.PUBLISHED, TargetStatus.ISOLATED, TargetStatus.ROLLED_BACK, TargetStatus.SKIPPED)
            for t in self.targets.values()
        )

    def rollback_finished(self) -> bool:
        return all(
            t.status in (TargetStatus.ROLLED_BACK, TargetStatus.SKIPPED, TargetStatus.ISOLATED)
            for t in self.targets.values()
        )

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "base_entity_id": self.base_entity_id,
            "base_revision": self.base_revision,
            "snapshot_digest": self.snapshot_digest,
            "locked_dependencies": self.locked_dependencies,
            "phase": self.phase,
            "created_at": self.created_at.isoformat(),
            "targets": [t.to_dict() for t in self.targets.values()],
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "ReleaseBatch":
        return cls(
            batch_id=data["batch_id"],
            base_entity_id=data["base_entity_id"],
            base_revision=data["base_revision"],
            snapshot_digest=data["snapshot_digest"],
            locked_dependencies=dict(data["locked_dependencies"]),
            phase=data["phase"],
            created_at=datetime.fromisoformat(data["created_at"]),
            targets={t["locale_code"]: LocaleTarget.from_dict(t) for t in data["targets"]},
        )


def _next_stage(current: str, order: tuple[str, ...]) -> str:
    if not current:
        return order[0]
    return order[order.index(current) + 1]


class ReleaseEngine:
    """推进批次阶段流水线；阶段完成即持久化检查点，保证可接续。"""

    def __init__(
        self,
        branches: Mapping[str, LocaleBranch],
        snapshots: Mapping[int, ContentSnapshot],
        approvals: ApprovalService,
        registry: ReleaseRegistry | None = None,
        checkpoint_sink: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._branches = branches
        self._snapshots = snapshots
        self._approvals = approvals
        self.registry = registry or ReleaseRegistry()
        self._checkpoint_sink = checkpoint_sink or (lambda: None)
        self._clock = clock
        # 一次性故障注入：(地区, 阶段) -> 原因，用于模拟基础设施失败
        self.faults: dict[tuple[str, str], str] = {}

    def inject_fault(self, locale_code: str, stage: str, reason: str) -> None:
        self.faults[(locale_code, stage)] = reason

    def _checkpoint(self) -> None:
        self._checkpoint_sink()

    # ---- 阻断原因 ----

    def blockers_for(self, branch: LocaleBranch, snapshot: ContentSnapshot) -> tuple[str, ...]:
        blockers: list[str] = []
        for change in branch.changes.values():
            if change.status == ChangeStatus.STALE:
                blockers.append(f"变更 {change.change_id}（{change.change_type}）已过期：{change.staleness_note}")
            elif change.status == ChangeStatus.CONFLICTED:
                blockers.append(f"变更 {change.change_id}（{change.change_type}）存在未解决差异：{change.staleness_note}")
            elif change.status == ChangeStatus.REJECTED:
                blockers.append(f"变更 {change.change_id}（{change.change_type}）已被驳回")
            elif not change.is_resolved:
                missing = "、".join(sorted(self._approvals.missing_roles(change)))
                blockers.append(
                    f"变更 {change.change_id}（{change.change_type}）审批未完成，缺少：{missing}"
                )
        ok, reason = self._approvals.school_confirmation_state(branch)
        if not ok:
            blockers.append(reason)
        locked = {d.name: d.version for d in snapshot.dependencies}
        if branch.base_revision != snapshot.revision:
            blockers.append(
                f"分支基于修订 {branch.base_revision}，批准的基础版本为修订 {snapshot.revision}，须先继承上游变更"
            )
        return tuple(blockers)

    # ---- 批次生命周期 ----

    def create_batch(self, batch_id: str, locale_codes: tuple[str, ...], snapshot: ContentSnapshot) -> ReleaseBatch:
        if not locale_codes:
            raise ValueError("发布批次至少包含一个地区")
        batch = ReleaseBatch(
            batch_id=batch_id,
            base_entity_id=snapshot.entity_id,
            base_revision=snapshot.revision,
            snapshot_digest=snapshot.digest,
            locked_dependencies={d.name: d.version for d in snapshot.dependencies},
            targets={code: LocaleTarget(code) for code in locale_codes},
        )
        self._checkpoint()
        return batch

    def run(self, batch: ReleaseBatch) -> None:
        """推进批次；崩溃重启后再次调用即从检查点接续。"""
        if batch.phase == BatchPhase.ROLLBACK:
            self._continue_rollback(batch)
            return
        for target in batch.targets.values():
            if target.isolated:
                continue
            if target.status in (TargetStatus.PENDING, TargetStatus.PUBLISHING, TargetStatus.FAILED):
                self._advance_publish(batch, target)
        if batch.is_finished():
            batch.phase = BatchPhase.COMPLETE
        self._checkpoint()

    def retry_locale(self, batch: ReleaseBatch, locale_code: str) -> None:
        """隔离后单独重试：从失败的阶段重新进入。"""
        target = batch.target(locale_code)
        if target.status not in (TargetStatus.FAILED, TargetStatus.ISOLATED):
            raise InvalidReleaseState(f"地区 {locale_code} 当前状态不可重试")
        target.isolated = False
        self._advance_publish(batch, target)
        if batch.is_finished() and batch.phase != BatchPhase.ROLLBACK:
            batch.phase = BatchPhase.COMPLETE
        self._checkpoint()

    def isolate_locale(self, batch: ReleaseBatch, locale_code: str) -> None:
        """把失败地区隔离出批次，其他地区可继续完成。"""
        target = batch.target(locale_code)
        if target.status != TargetStatus.FAILED:
            raise InvalidReleaseState(f"地区 {locale_code} 未失败，无需隔离")
        target.status = TargetStatus.ISOLATED
        target.isolated = True
        if batch.is_finished():
            batch.phase = BatchPhase.COMPLETE
        self._checkpoint()

    # ---- 发布流水线 ----

    def _advance_publish(self, batch: ReleaseBatch, target: LocaleTarget) -> None:
        branch = self._branches[target.locale_code]
        snapshot = self._snapshots[batch.base_revision]
        target.status = TargetStatus.PUBLISHING
        target.attempts += 1
        # 新一轮尝试：阻断原因重新计算，不残留上一轮的结论
        target.blockers = ()
        while target.stage != PublishStage.DONE:
            stage = _next_stage(target.stage, PublishStage.ORDER)
            self._run_publish_stage(batch, target, branch, snapshot, stage)
            if target.status == TargetStatus.FAILED:
                self._checkpoint()
                return
            target.stage = stage
            self._checkpoint()
        target.status = TargetStatus.PUBLISHED

    def _run_publish_stage(
        self,
        batch: ReleaseBatch,
        target: LocaleTarget,
        branch: LocaleBranch,
        snapshot: ContentSnapshot,
        stage: str,
    ) -> None:
        fault = self.faults.pop((target.locale_code, stage), None)
        if fault is not None:
            target.status = TargetStatus.FAILED
            target.blockers = (*target.blockers, f"{stage}失败：{fault}")
            return
        if stage == PublishStage.BLOCKED_CHECK:
            blockers = self.blockers_for(branch, snapshot)
            if blockers:
                target.status = TargetStatus.FAILED
                target.blockers = blockers
                return
        elif stage == PublishStage.LOCK:
            # 锁定内容摘要与依赖版本：此后批次内看到的内容不再随分支变化
            target.locked_content_digest = branch.content_digest()
        elif stage == PublishStage.COMMIT:
            rec = ReleaseRecord(
                locale_code=target.locale_code,
                batch_id=batch.batch_id,
                base_revision=batch.base_revision,
                snapshot_digest=batch.snapshot_digest,
                content_digest=target.locked_content_digest,
                dependencies=tuple(sorted(batch.locked_dependencies.items())),
                published_at=self._clock(),
            )
            self.registry.record_release(rec)

    # ---- 紧急撤回流水线 ----

    def emergency_recall(self, batch: ReleaseBatch) -> None:
        """发起紧急撤回：已发布地区回到最近兼容版本并等待院校确认。

        尚未发布成功的地区标记为“未参与撤回”。可在重启后接续执行。
        """
        batch.phase = BatchPhase.ROLLBACK
        for target in batch.targets.values():
            if target.status == TargetStatus.PUBLISHED:
                target.status = TargetStatus.ROLLING_BACK
            elif target.status in (TargetStatus.FAILED, TargetStatus.PENDING, TargetStatus.PUBLISHING):
                target.status = TargetStatus.SKIPPED
            # ISOLATED / ROLLED_BACK 保持原样
        self._checkpoint()
        self._continue_rollback(batch)

    def _continue_rollback(self, batch: ReleaseBatch) -> None:
        for target in batch.targets.values():
            if target.status != TargetStatus.ROLLING_BACK:
                continue
            self._advance_rollback(batch, target)
        if batch.rollback_finished():
            batch.phase = BatchPhase.COMPLETE
        self._checkpoint()

    def _advance_rollback(self, batch: ReleaseBatch, target: LocaleTarget) -> None:
        branch = self._branches[target.locale_code]
        while target.rollback_stage != RollbackStage.DONE:
            stage = _next_stage(target.rollback_stage, RollbackStage.ORDER)
            fault = self.faults.pop((target.locale_code, stage), None)
            if fault is not None:
                target.status = TargetStatus.ROLLING_BACK
                self._checkpoint()
                return
            if stage == RollbackStage.RECALL:
                pass  # 下线当前批次版本
            elif stage == RollbackStage.RESTORE:
                current = self.registry.current(target.locale_code)
                if current is None or current.batch_id != batch.batch_id:
                    target.status = TargetStatus.FAILED
                    target.blockers = (*target.blockers, "撤回异常：当前生效版本不属于本批次")
                    return
                fallback = self.registry.latest_compatible(
                    target.locale_code,
                    batch.locked_dependencies,
                    exclude_batch_id=batch.batch_id,
                )
                if fallback is None:
                    target.status = TargetStatus.FAILED
                    target.blockers = (*target.blockers, "没有可回退的最近兼容版本")
                    return
                self.registry.replace_current(target.locale_code, fallback)
                target.restored_to = f"批次 {fallback.batch_id} / 修订 {fallback.base_revision}"
            elif stage == RollbackStage.NOTIFY:
                # 回退内容与原确认覆盖的内容不同，院校确认随即失效，必须等待重新确认
                target.blockers = ("已回退到兼容版本，等待合作院校重新确认",)
            target.rollback_stage = stage
            self._checkpoint()
        target.status = TargetStatus.ROLLED_BACK


class InvalidReleaseState(Exception):
    """批次/地区状态不允许该操作。"""
