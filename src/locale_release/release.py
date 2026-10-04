"""发布批次、地区任务、检查点与紧急撤回。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ReleaseTaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class DependencyPin:
    """发布时锁定的覆盖项依赖版本。"""

    override_id: str
    revision: int


@dataclass
class ReleaseTask:
    """批次内某一地区的发布任务，失败可隔离重试。"""

    region: str
    content_digest: str
    dependency_pins: tuple[DependencyPin, ...]
    state: ReleaseTaskState = ReleaseTaskState.PENDING
    attempts: int = 0
    last_error: Optional[str] = None


@dataclass
class ReleaseBatch:
    """发布批次：锁定各地区内容摘要与依赖版本。"""

    batch_id: str
    base_revision: int
    base_digest: str
    created_at: datetime
    tasks: dict[str, ReleaseTask] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return bool(self.tasks) and all(
            t.state == ReleaseTaskState.SUCCEEDED for t in self.tasks.values()
        )


@dataclass(frozen=True)
class Checkpoint:
    """发布/撤回过程的追加式检查点，供重启后的进程续接。"""

    seq: int
    process_id: str
    phase: str  # publish / rollback
    event: str
    region: str
    at: datetime
    detail: str = ""


@dataclass
class PublishedRelease:
    """某地区一次成功发布留下的可用版本。"""

    region: str
    batch_id: str
    content_digest: str
    base_revision: int
    dependency_pins: tuple[DependencyPin, ...]
    published_at: datetime
    withdrawn: bool = False


class RollbackState(str, Enum):
    AWAITING_SCHOOL_CONFIRMATION = "awaiting_school_confirmation"
    CONFIRMED = "confirmed"


@dataclass
class RollbackRecord:
    """紧急撤回记录：回到最近兼容版本并等待学校确认。"""

    rollback_id: str
    region: str
    from_batch_id: str
    to_batch_id: str
    to_digest: str
    state: RollbackState
    initiated_at: datetime
    confirmed_at: Optional[datetime] = None
    confirmed_by: Optional[str] = None
