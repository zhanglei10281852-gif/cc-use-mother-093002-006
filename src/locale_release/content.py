"""内容单元、快照与依赖版本。

发布批次锁定的“内容摘要”即快照摘要（digest），
保证同一批次在任何时刻重算都得到完全一致的内容指纹。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class ContentUnit:
    unit_id: str
    title: str
    body: str

    def __post_init__(self) -> None:
        if not self.unit_id or not self.title:
            raise ValueError("内容单元信息不完整")

    def digest(self) -> str:
        raw = json.dumps(
            {"unit_id": self.unit_id, "title": self.title, "body": self.body},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {"unit_id": self.unit_id, "title": self.title, "body": self.body}

    @classmethod
    def from_dict(cls, data: Mapping[str, str]) -> "ContentUnit":
        return cls(data["unit_id"], data["title"], data["body"])


@dataclass(frozen=True)
class DependencyVersion:
    """基础课程依赖的外部资源版本（媒体库、词表包等）。"""

    name: str
    version: str

    def __post_init__(self) -> None:
        if not self.name or not self.version:
            raise ValueError("依赖版本信息不完整")

    @property
    def major(self) -> str:
        return self.version.split(".", 1)[0]

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version}

    @classmethod
    def from_dict(cls, data: Mapping[str, str]) -> "DependencyVersion":
        return cls(data["name"], data["version"])


@dataclass(frozen=True)
class ContentSnapshot:
    """一个经过批准的基础课程版本的不可变快照。"""

    entity_id: str
    revision: int
    units: tuple[ContentUnit, ...]
    dependencies: tuple[DependencyVersion, ...] = field(default=())
    digest: str = ""

    def __post_init__(self) -> None:
        if not self.entity_id or self.revision < 1 or not self.units:
            raise ValueError("基础课程快照不合法")
        object.__setattr__(self, "digest", self._compute_digest())

    def _compute_digest(self) -> str:
        raw = json.dumps(
            {
                "entity_id": self.entity_id,
                "revision": self.revision,
                "units": [u.to_dict() for u in sorted(self.units, key=lambda u: u.unit_id)],
                "dependencies": [d.to_dict() for d in sorted(self.dependencies, key=lambda d: d.name)],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def unit(self, unit_id: str) -> ContentUnit | None:
        for u in self.units:
            if u.unit_id == unit_id:
                return u
        return None

    def changed_unit_ids(self, previous: "ContentSnapshot | None") -> frozenset[str]:
        """相对上一版本被修改或删除的内容单元（新增单元不影响既有覆盖）。"""
        if previous is None:
            return frozenset(u.unit_id for u in self.units)
        old = {u.unit_id: u.digest() for u in previous.units}
        current = {u.unit_id: u.digest() for u in self.units}
        changed = {uid for uid, digest in old.items() if current.get(uid) != digest}
        return frozenset(changed)

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "revision": self.revision,
            "units": [u.to_dict() for u in self.units],
            "dependencies": [d.to_dict() for d in self.dependencies],
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "ContentSnapshot":
        return cls(
            entity_id=data["entity_id"],
            revision=data["revision"],
            units=tuple(ContentUnit.from_dict(u) for u in data["units"]),
            dependencies=tuple(DependencyVersion.from_dict(d) for d in data.get("dependencies", ())),
        )
