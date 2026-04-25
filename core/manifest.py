"""技能清单 (skill.yaml) 解析。

每个技能必须有一个 skill.yaml，结构：
    name: echo                       # 唯一标识，必填
    display_name: Echo               # 展示名（可选）
    version: 1.0.0                   # 版本（可选）
    author: someone                  # 作者（可选）
    description: 回显输入             # 描述（可选）
    command: echo                    # 触发指令（可选；缺省则用 name）
    entrypoint: handler:run          # "<模块>:<函数>"，必填
    timeout_seconds: 10              # 单次执行超时（可选）
    permission: user                 # user | admin（可选，默认 user）
    loopable: true                   # 是否允许挂 loop（可选，默认 true）
    max_loop_instances: 0            # 同一技能最多并存的 loop 数（可选，0=不限）

    default_loops:                   # 可选：自动注册的 loop 模板（无 target_session，需管理员领养）
      - id: heartbeat                # 短 id；最终 LoopSpec.id = tpl_<skill_name>_<id>
        interval_seconds: 60
        args: []
      - id: morning
        cron_expr: "0 9 * * *"
        args: ["status"]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ManifestError(ValueError):
    """skill.yaml 解析或校验失败。"""


_PERMISSIONS = frozenset({"user", "admin"})


@dataclass(frozen=True)
class LoopTemplate:
    """skill.yaml 中声明的 loop 模板（无 target_session）。

    加载时框架会以 deterministic id `tpl_<skill_name>_<id_suffix>` 注册为
    暂停状态的 loop；管理员可通过 /skill loop adopt 在某个会话里"领养"它。
    """

    id_suffix: str
    interval_seconds: int = 0
    cron_expr: str = ""
    jitter_seconds: int = 0
    args: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class SkillManifest:
    """已校验的技能清单。所有字段都是不可变的。"""

    name: str
    entrypoint_module: str
    entrypoint_func: str
    skill_dir: Path
    display_name: str = ""
    version: str = "0.0.0"
    author: str = ""
    description: str = ""
    command: str = ""
    timeout_seconds: int | None = None
    permission: str = "user"
    loopable: bool = True
    max_loop_instances: int = 0
    default_loops: tuple[LoopTemplate, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def trigger(self) -> str:
        """实际触发指令字符串。"""
        return self.command or self.name


def load_manifest(skill_dir: Path) -> SkillManifest:
    """从指定技能目录加载并校验 skill.yaml。

    Raises:
        ManifestError: 文件缺失、YAML 无法解析或必填字段缺失/非法。
    """
    yaml_path = skill_dir / "skill.yaml"
    if not yaml_path.is_file():
        raise ManifestError(f"未找到 skill.yaml: {yaml_path}")

    try:
        raw_text = yaml_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ManifestError(f"读取 {yaml_path} 失败: {e}") from e

    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as e:
        raise ManifestError(f"{yaml_path} YAML 解析失败: {e}") from e

    if not isinstance(data, dict):
        raise ManifestError(f"{yaml_path} 顶层必须是字典")

    name = _require_str(data, "name")
    if not _is_safe_identifier(name):
        raise ManifestError(
            f"name 必须是 [A-Za-z0-9_-] 组成且不超过 64 字符: {name!r}"
        )

    entrypoint = _require_str(data, "entrypoint")
    if ":" not in entrypoint:
        raise ManifestError(
            f"entrypoint 必须形如 '模块:函数'，当前: {entrypoint!r}"
        )
    module_part, func_part = entrypoint.split(":", 1)
    module_part = module_part.strip()
    func_part = func_part.strip()
    if not module_part or not func_part:
        raise ManifestError(f"entrypoint 模块名或函数名为空: {entrypoint!r}")

    permission = str(data.get("permission", "user")).strip() or "user"
    if permission not in _PERMISSIONS:
        raise ManifestError(
            f"permission 取值非法（应为 user/admin）: {permission!r}"
        )

    timeout = data.get("timeout_seconds")
    if timeout is not None:
        if not isinstance(timeout, int) or timeout <= 0:
            raise ManifestError(
                f"timeout_seconds 必须是正整数: {timeout!r}"
            )

    loopable = data.get("loopable", True)
    if not isinstance(loopable, bool):
        raise ManifestError(f"loopable 必须是布尔: {loopable!r}")

    max_loop_instances = data.get("max_loop_instances", 0)
    if not isinstance(max_loop_instances, int) or max_loop_instances < 0:
        raise ManifestError(
            f"max_loop_instances 必须是非负整数: {max_loop_instances!r}"
        )

    default_loops = _parse_default_loops(data.get("default_loops"), name)

    return SkillManifest(
        name=name,
        entrypoint_module=module_part,
        entrypoint_func=func_part,
        skill_dir=skill_dir,
        display_name=str(data.get("display_name", "")).strip(),
        version=str(data.get("version", "0.0.0")).strip(),
        author=str(data.get("author", "")).strip(),
        description=str(data.get("description", "")).strip(),
        command=str(data.get("command", "")).strip(),
        timeout_seconds=timeout,
        permission=permission,
        loopable=loopable,
        max_loop_instances=max_loop_instances,
        default_loops=default_loops,
        raw=data,
    )


def _parse_default_loops(
    raw: object, skill_name: str
) -> tuple[LoopTemplate, ...]:
    """解析 manifest.default_loops 列表。失败时抛 ManifestError。"""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError("default_loops 必须是列表")
    seen_ids: set[str] = set()
    templates: list[LoopTemplate] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ManifestError(f"default_loops[{i}] 必须是字典")
        sub_id = str(item.get("id", "")).strip()
        if not sub_id:
            raise ManifestError(f"default_loops[{i}].id 缺失")
        if not _is_safe_identifier(sub_id):
            raise ManifestError(
                f"default_loops[{i}].id 必须由 [A-Za-z0-9_-] 组成: {sub_id!r}"
            )
        if sub_id in seen_ids:
            raise ManifestError(f"default_loops 中存在重复 id: {sub_id!r}")
        seen_ids.add(sub_id)

        interval = item.get("interval_seconds", 0)
        cron = str(item.get("cron_expr", "")).strip()
        if cron and interval:
            raise ManifestError(
                f"default_loops[{sub_id}]: cron_expr 与 interval_seconds 互斥"
            )
        if not cron:
            if not isinstance(interval, int) or interval <= 0:
                raise ManifestError(
                    f"default_loops[{sub_id}].interval_seconds 必须是正整数"
                )
        jitter = item.get("jitter_seconds", 0)
        if not isinstance(jitter, int) or jitter < 0:
            raise ManifestError(
                f"default_loops[{sub_id}].jitter_seconds 必须是非负整数"
            )

        args_raw = item.get("args", []) or []
        if not isinstance(args_raw, list):
            raise ManifestError(f"default_loops[{sub_id}].args 必须是列表")
        args = tuple(str(a) for a in args_raw)

        templates.append(
            LoopTemplate(
                id_suffix=sub_id,
                interval_seconds=int(interval) if not cron else 0,
                cron_expr=cron,
                jitter_seconds=int(jitter),
                args=args,
                description=str(item.get("description", "")).strip(),
            )
        )
    return tuple(templates)


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"必填字段缺失或非字符串: {key}")
    return value.strip()


def _is_safe_identifier(name: str) -> bool:
    if not name or len(name) > 64:
        return False
    return all(c.isalnum() or c in {"_", "-"} for c in name)
