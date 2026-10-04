"""状态查询：地区继承差异对比、阻断原因与重启接续位置。"""
from __future__ import annotations

from dataclasses import dataclass

from .localization import LocaleBranch
from .release import (
    BatchPhase,
    LocaleTarget,
    PublishStage,
    ReleaseBatch,
    RollbackStage,
    TargetStatus,
)


@dataclass(frozen=True)
class ChangeDiff:
    locale_a: str
    locale_b: str
    based_on_same_revision: bool
    base_revision_a: int
    base_revision_b: int
    pending_revision_a: int | None
    pending_revision_b: int | None
    inheritance_in_sync: bool
    common_change_ids: tuple[str, ...]
    only_in_a: tuple[str, ...]
    only_in_b: tuple[str, ...]
    payload_diverged: tuple[str, ...]  # 同一变更目标在两地的覆盖内容不同
    status_diverged: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "locale_a": self.locale_a,
            "locale_b": self.locale_b,
            "based_on_same_revision": self.based_on_same_revision,
            "base_revision_a": self.base_revision_a,
            "base_revision_b": self.base_revision_b,
            "pending_revision_a": self.pending_revision_a,
            "pending_revision_b": self.pending_revision_b,
            "inheritance_in_sync": self.inheritance_in_sync,
            "common_change_ids": list(self.common_change_ids),
            "only_in_a": list(self.only_in_a),
            "only_in_b": list(self.only_in_b),
            "payload_diverged": list(self.payload_diverged),
            "status_diverged": list(self.status_diverged),
        }


def _change_key(change) -> tuple[str, str]:
    """以“变更类型 + 目标单元”判定两地是否覆盖了同一件事。"""
    return (change.change_type, change.target_unit_id)


def compare_locales(branch_a: LocaleBranch, branch_b: LocaleBranch) -> ChangeDiff:
    """直接比较两个地区的继承差异。

    base_* 是两地实际继承的修订；pending_* 是已计算影响、尚未完成
    解决与继承的修订；任一地存在待继承或两地基准不同即视为未同步。
    """
    by_key_a = {_change_key(c): c for c in branch_a.changes.values()}
    by_key_b = {_change_key(c): c for c in branch_b.changes.values()}
    keys_a, keys_b = set(by_key_a), set(by_key_b)

    payload_diverged: list[str] = []
    status_diverged: list[str] = []
    for key in sorted(keys_a & keys_b):
        a, b = by_key_a[key], by_key_b[key]
        if a.payload != b.payload or a.based_on_revision != b.based_on_revision:
            payload_diverged.append(f"{key[0]}→{key[1]}")
        if a.status != b.status:
            status_diverged.append(f"{key[0]}→{key[1]}：{a.status} vs {b.status}")

    def describe(mapping, keys):
        return tuple(f"{k[0]}→{k[1]}（{mapping[k].change_id}）" for k in sorted(keys))

    return ChangeDiff(
        locale_a=branch_a.locale_code,
        locale_b=branch_b.locale_code,
        based_on_same_revision=branch_a.base_revision == branch_b.base_revision,
        base_revision_a=branch_a.base_revision,
        base_revision_b=branch_b.base_revision,
        pending_revision_a=branch_a.pending_revision,
        pending_revision_b=branch_b.pending_revision,
        inheritance_in_sync=(
            branch_a.base_revision == branch_b.base_revision
            and branch_a.pending_revision == branch_b.pending_revision
        ),
        common_change_ids=tuple(sorted({k[1] for k in keys_a & keys_b})),
        only_in_a=describe(by_key_a, keys_a - keys_b),
        only_in_b=describe(by_key_b, keys_b - keys_a),
        payload_diverged=tuple(payload_diverged),
        status_diverged=tuple(status_diverged),
    )


def resume_position(batch: ReleaseBatch, locale_code: str) -> dict:
    """说明重新启动的发布进程应从哪里接续。"""
    target = batch.target(locale_code)
    if batch.phase == BatchPhase.ROLLBACK and target.status == TargetStatus.ROLLING_BACK:
        order = RollbackStage.ORDER
        current = target.rollback_stage
        pipeline = "紧急撤回"
    elif target.status in (TargetStatus.PUBLISHING, TargetStatus.FAILED):
        order = PublishStage.ORDER
        current = target.stage
        pipeline = "发布"
    else:
        return {
            "locale_code": locale_code,
            "pipeline": "发布" if batch.phase != BatchPhase.ROLLBACK else "紧急撤回",
            "resume_from": None,
            "note": f"当前状态“{target.status}”无需接续",
        }
    # 检查点记录的是“已完成的最后阶段”，重启后从下一阶段重入；
    # 已抵达末阶段则没有需要接续的阶段。
    if not current:
        resume_from = order[0]
    elif current == order[-1]:
        resume_from = None
    else:
        resume_from = order[order.index(current) + 1]
    return {
        "locale_code": locale_code,
        "pipeline": pipeline,
        "last_completed_stage": current or None,
        "resume_from": resume_from,
        "attempts": target.attempts,
    }


def target_report(batch: ReleaseBatch, locale_code: str) -> dict:
    """单个地区的状态查询：状态、锁定摘要、阻断原因、接续位置。"""
    target: LocaleTarget = batch.target(locale_code)
    return {
        "batch_id": batch.batch_id,
        "locale_code": locale_code,
        "status": target.status,
        "publish_stage": target.stage or None,
        "rollback_stage": target.rollback_stage or None,
        "isolated": target.isolated,
        "attempts": target.attempts,
        "locked_content_digest": target.locked_content_digest or None,
        "locked_dependencies": dict(batch.locked_dependencies),
        "restored_to": target.restored_to or None,
        "blockers": list(target.blockers),
        "resume": resume_position(batch, locale_code),
    }
