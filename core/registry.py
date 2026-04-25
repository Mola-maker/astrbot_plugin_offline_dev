"""内存里的技能注册表。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .manifest import SkillManifest


@dataclass(frozen=True)
class RegisteredSkill:
    """已注册的技能（manifest + handler 引用）。"""

    manifest: SkillManifest
    handler: Callable
    data_dir: Path

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def trigger(self) -> str:
        return self.manifest.trigger


class SkillRegistry:
    """以 name 为主键的技能注册表，附带 trigger 反查索引。"""

    def __init__(self) -> None:
        self._by_name: dict[str, RegisteredSkill] = {}
        self._by_trigger: dict[str, str] = {}

    def replace_all(self, skills: list[RegisteredSkill]) -> None:
        """整体替换：注册新表，丢弃旧表。"""
        new_by_name: dict[str, RegisteredSkill] = {}
        new_by_trigger: dict[str, str] = {}
        for skill in skills:
            new_by_name[skill.name] = skill
            # 同名 trigger 后注册的覆盖前一个；调用方应预先保证 manifest.name 唯一
            new_by_trigger[skill.trigger] = skill.name
        self._by_name = new_by_name
        self._by_trigger = new_by_trigger

    def get(self, name: str) -> RegisteredSkill | None:
        return self._by_name.get(name)

    def get_by_trigger(self, trigger: str) -> RegisteredSkill | None:
        name = self._by_trigger.get(trigger)
        if name is None:
            return None
        return self._by_name.get(name)

    def names(self) -> list[str]:
        return sorted(self._by_name.keys())

    def __len__(self) -> int:
        return len(self._by_name)

    def __iter__(self) -> Iterator[RegisteredSkill]:
        return iter(self._by_name.values())
