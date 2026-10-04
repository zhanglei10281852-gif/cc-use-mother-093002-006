"""区域课程本地化发布平台门面。

聚合基础课程快照、地区分支、审批服务、发布历史与批次，
并以单个 JSON 文件持久化；发布进程重启后载入即可从检查点接续。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from .approval import ApprovalService
from .content import ContentSnapshot
from .localization import LocaleBranch, LocalizationChange
from .release import ReleaseBatch, ReleaseEngine, ReleaseRegistry
from .status import compare_locales, resume_position, target_report


class LocalizationPlatform:
    def __init__(
        self,
        store_path: str | Path | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._store_path = Path(store_path) if store_path else None
        self._clock = clock
        self.snapshots: dict[int, ContentSnapshot] = {}
        self.branches: dict[str, LocaleBranch] = {}
        self.approvals = ApprovalService(clock=clock)
        self.registry = ReleaseRegistry()
        self.batches: dict[str, ReleaseBatch] = {}
        if self._store_path and self._store_path.exists():
            self.load()
        self.engine = ReleaseEngine(
            branches=self.branches,
            snapshots=self.snapshots,
            approvals=self.approvals,
            registry=self.registry,
            checkpoint_sink=self.save,
            clock=clock,
        )

    # ---- 基础课程与地区分支 ----

    def register_base_snapshot(self, snapshot: ContentSnapshot) -> None:
        self.snapshots[snapshot.revision] = snapshot
        self.save()

    def latest_revision(self) -> int:
        return max(self.snapshots)

    def onboard_locale(self, locale_code: str, revision: int | None = None) -> LocaleBranch:
        revision = revision or self.latest_revision()
        if revision not in self.snapshots:
            raise KeyError(f"基础课程修订 {revision} 不存在")
        branch = LocaleBranch(
            locale_code=locale_code,
            base_entity_id=self.snapshots[revision].entity_id,
            base_revision=revision,
        )
        self.branches[locale_code] = branch
        self.save()
        return branch

    def submit_change(self, locale_code: str, change: LocalizationChange) -> None:
        branch = self.branches[locale_code]
        if change.based_on_revision != branch.base_revision:
            raise ValueError("变更所基于的修订与分支当前继承的修订不一致")
        branch.add_change(change)
        self.save()

    def promote_base(self, snapshot: ContentSnapshot) -> dict[str, list[str]]:
        """总部发布新的批准基础版本：自动计算对各地区分支的影响。"""
        previous = self.snapshots.get(max(self.snapshots)) if self.snapshots else None
        self.snapshots[snapshot.revision] = snapshot
        result: dict[str, list[str]] = {}
        for branch in self.branches.values():
            affected = branch.apply_upstream(snapshot, previous)
            self.approvals.invalidate_stale_approvals(affected)
            if affected:
                result[branch.locale_code] = [c.change_id for c in affected]
        self.save()
        return result

    def reconfirm_change(self, locale_code: str, change_id: str, note: str = "") -> None:
        """地区确认过期/差异变更仍适用，随后须重新完成审批。

        确认基准为待继承的上游修订（若有），否则为当前修订。
        """
        branch = self.branches[locale_code]
        target_revision = branch.pending_revision or branch.base_revision
        branch.change(change_id).confirm_against(target_revision, note)
        self.save()

    def inherit_upstream(self, locale_code: str) -> int:
        """全部差异解决并重走审批后，把分支继承到新的基础修订。"""
        revision = self.branches[locale_code].inherit_pending_revision()
        self.save()
        return revision

    # ---- 审批与院校确认 ----

    def approve_change(self, locale_code: str, change_id: str, role: str, approver_id: str) -> None:
        self.approvals.approve(self.branches[locale_code], change_id, role, approver_id)
        self.save()

    def reject_change(self, locale_code: str, change_id: str, role: str, approver_id: str) -> None:
        self.approvals.reject(self.branches[locale_code], change_id, role, approver_id)
        self.save()

    def confirm_school(self, locale_code: str, confirmed_by: str) -> None:
        self.approvals.confirm_school(self.branches[locale_code], confirmed_by)
        self.save()

    # ---- 发布 ----

    def create_batch(self, batch_id: str, locale_codes: tuple[str, ...], revision: int | None = None) -> ReleaseBatch:
        snapshot = self.snapshots[revision or self.latest_revision()]
        batch = self.engine.create_batch(batch_id, locale_codes, snapshot)
        self.batches[batch_id] = batch
        self.save()
        return batch

    def run_batch(self, batch_id: str) -> None:
        """推进批次；进程重启后调用同一方法即从原执行位置接续。"""
        self.engine.run(self.batches[batch_id])

    def isolate_locale(self, batch_id: str, locale_code: str) -> None:
        self.engine.isolate_locale(self.batches[batch_id], locale_code)

    def retry_locale(self, batch_id: str, locale_code: str) -> None:
        self.engine.retry_locale(self.batches[batch_id], locale_code)

    def emergency_recall(self, batch_id: str) -> None:
        self.engine.emergency_recall(self.batches[batch_id])

    # ---- 查询 ----

    def compare(self, locale_a: str, locale_b: str) -> dict:
        return compare_locales(self.branches[locale_a], self.branches[locale_b]).to_dict()

    def status(self, batch_id: str, locale_code: str) -> dict:
        return target_report(self.batches[batch_id], locale_code)

    def resume(self, batch_id: str, locale_code: str) -> dict:
        return resume_position(self.batches[batch_id], locale_code)

    # ---- 持久化（检查点） ----

    def save(self) -> None:
        if self._store_path is None:
            return
        payload = {
            "snapshots": {str(rev): s.to_dict() for rev, s in self.snapshots.items()},
            "branches": [b.to_dict() for b in self.branches.values()],
            "approvals": self.approvals.to_dict(),
            "registry": self.registry.to_dict(),
            "batches": [b.to_dict() for b in self.batches.values()],
        }
        tmp = self._store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._store_path)

    def load(self) -> None:
        data = json.loads(self._store_path.read_text(encoding="utf-8"))
        self.snapshots = {
            int(rev): ContentSnapshot.from_dict(raw) for rev, raw in data.get("snapshots", {}).items()
        }
        self.branches = {b["locale_code"]: LocaleBranch.from_dict(b) for b in data.get("branches", ())}
        self.approvals = ApprovalService.from_dict(data.get("approvals", {}), clock=self._clock)
        self.registry = ReleaseRegistry.from_dict(data.get("registry", {}))
        self.batches = {b["batch_id"]: ReleaseBatch.from_dict(b) for b in data.get("batches", ())}
        self.engine = ReleaseEngine(
            branches=self.branches,
            snapshots=self.snapshots,
            approvals=self.approvals,
            registry=self.registry,
            checkpoint_sink=self.save,
            clock=self._clock,
        )
