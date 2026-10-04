"""区域课程本地化发布平台：端到端冒烟演示。

覆盖流程：登记基础版本 → 地区提交覆盖项 → 组合审批 → 上游修订影响分析 →
冲突解决 → 发布批次（锁定摘要与依赖版本）→ 失败隔离与重启续接 →
紧急撤回与学校确认 → 状态查询。
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from locale_release import (  # noqa: E402
    ApproverRole,
    BaseCourseSnapshot,
    BaseCourseVersion,
    ChangeType,
    ContentItem,
    LocalizationService,
)

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
service = LocalizationService(clock=lambda: NOW)
stages = []


def stage(name, **payload):
    stages.append({"stage": name, **payload})


# 1. 登记经过批准的基础课程版本 R1
service.publish_base_snapshot(
    BaseCourseSnapshot(
        BaseCourseVersion("COURSE-ZH", "国际中文职业课程", 1),
        (
            ContentItem("term:薪资谈判", ChangeType.TERMINOLOGY, "薪资谈判"),
            ContentItem("case:职场对话", ChangeType.CASE, "总部案例文本"),
            ContentItem("lesson:3", ChangeType.SCHEDULE, "45分钟"),
        ),
        approved=True,
    )
)
service.open_branch("SG")
service.open_branch("TH")
stage("base_registered", revision=1, regions=["SG", "TH"])

# 2. 地区提交覆盖项并按变更类型完成组合审批
service.submit_override("OV-SG-1", "SG", ChangeType.TERMINOLOGY, "term:薪资谈判", "薪酬商谈")
service.record_approval("AP-1", "OV-SG-1", ApproverRole.LANGUAGE_EXPERT, "语言专家甲")
service.record_approval("AP-2", "OV-SG-1", ApproverRole.SUBJECT_LEAD, "学科负责人乙")
service.finalize_override("OV-SG-1")

service.submit_override("OV-TH-1", "TH", ChangeType.CASE, "case:职场对话", "泰国本地案例")
service.record_approval("AP-3", "OV-TH-1", ApproverRole.SUBJECT_LEAD, "学科负责人乙")
service.record_approval("AP-4", "OV-TH-1", ApproverRole.PARTNER_SCHOOL, "曼谷合作院校")
service.finalize_override("OV-TH-1")
stage("overrides_approved", overrides=["OV-SG-1", "OV-TH-1"])

# 3. 总部推进 R2：修订了 SG 覆盖的术语键，自动计算各地影响
reports = service.publish_base_snapshot(
    BaseCourseSnapshot(
        BaseCourseVersion("COURSE-ZH", "国际中文职业课程", 2),
        (
            ContentItem("term:薪资谈判", ChangeType.TERMINOLOGY, "薪酬谈判"),
            ContentItem("case:职场对话", ChangeType.CASE, "总部案例文本"),
            ContentItem("lesson:3", ChangeType.SCHEDULE, "45分钟"),
        ),
        approved=True,
    )
)
stage(
    "impact_analyzed",
    **{
        region: [
            {"override": i.override_id, "kind": i.kind.value} for i in report.impacts
        ]
        for region, report in reports.items()
    },
)

# 4. SG 编辑解决冲突并重新审批；TH 的确认因继承源变化需重新确认
service.resolve_conflict("OV-SG-1", "edit", new_payload="薪酬协商")
service.record_approval("AP-5", "OV-SG-1", ApproverRole.LANGUAGE_EXPERT, "语言专家甲")
service.record_approval("AP-6", "OV-SG-1", ApproverRole.SUBJECT_LEAD, "学科负责人乙")
service.finalize_override("OV-SG-1")
service.record_approval("AP-7", "OV-TH-1", ApproverRole.SUBJECT_LEAD, "学科负责人乙")
service.record_approval("AP-8", "OV-TH-1", ApproverRole.PARTNER_SCHOOL, "曼谷合作院校")
service.finalize_override("OV-TH-1")
stage("conflicts_resolved")

# 5. 创建发布批次：锁定各地区内容摘要与依赖版本
batch = service.create_release_batch("BATCH-1", ["SG", "TH"])
stage(
    "batch_created",
    batch=batch.batch_id,
    base_revision=batch.base_revision,
    locks={
        region: {
            "digest": task.content_digest[:12] + "…",
            "pins": [f"{p.override_id}@v{p.revision}" for p in task.dependency_pins],
        }
        for region, task in batch.tasks.items()
    },
)

# 6. 执行批次：TH 首次交付失败被隔离；模拟进程重启后续接完成
attempts = {"TH": 1}


def flaky_deliver(region, task):
    if attempts.get(region):
        attempts[region] -= 1
        raise RuntimeError("交付通道超时")


service.execute_batch("BATCH-1", deliver=flaky_deliver)
stage(
    "batch_partial_failure",
    states={region: task.state.value for region, task in batch.tasks.items()},
)

restarted = LocalizationService(repo=service.repo, clock=lambda: NOW)
position = restarted.resume_position("BATCH-1")
stage(
    "resume_position",
    phase=position.phase,
    next_action=position.next_action,
    pending_regions=list(position.pending_regions),
)
restarted.resume_batch("BATCH-1")
stage(
    "batch_completed",
    states={region: task.state.value for region, task in batch.tasks.items()},
)

# 7. SG 第二次发布后紧急撤回：回到最近兼容版本并等待学校确认
service.submit_override("OV-SG-2", "SG", ChangeType.SCHEDULE, "lesson:3", "40分钟")
service.record_approval("AP-9", "OV-SG-2", ApproverRole.PARTNER_SCHOOL, "新加坡合作院校")
service.finalize_override("OV-SG-2")
service.create_release_batch("BATCH-2", ["SG"])
service.execute_batch("BATCH-2")

record = service.emergency_rollback("RB-1", "SG")
stage(
    "rollback_initiated",
    region=record.region,
    back_to=record.to_batch_id,
    state=record.state.value,
)
service.confirm_rollback("RB-1", "新加坡合作院校")
stage("rollback_confirmed", active_batch=service.active_release("SG").batch_id)

# 8. 状态查询：地区继承差异、阻断原因、进程续接位置
diff = service.compare_regions("SG", "TH")
stage(
    "region_diff",
    only_in_sg=list(diff.only_in_a),
    only_in_th=list(diff.only_in_b),
    differing=[d.target_key for d in diff.differing],
)
stage(
    "blocking_reasons",
    SG=[r.code for r in service.blocking_reasons("SG")],
    TH=[r.code for r in service.blocking_reasons("TH")],
)
stage(
    "final_resume",
    batch_1=service.resume_position("BATCH-1").phase,
    rollback_rb_1=service.resume_position("RB-1").phase,
)

print(json.dumps(stages, ensure_ascii=False, indent=2))
