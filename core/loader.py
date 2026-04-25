"""扫描技能根目录、动态导入 handler 模块。

每个技能目录形如:
    <skills_root>/<skill_name>/
        skill.yaml
        handler.py            # 或 manifest.entrypoint_module 指定的其他模块

为了不污染全局 sys.modules，所有技能模块以
    "astrbot_plugin_offline_dev._skills.<skill_name>.<module>"
为完整模块名注册，重新加载时会清理同前缀的旧模块。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable

from .manifest import ManifestError, SkillManifest, load_manifest

logger = logging.getLogger("astrbot_plugin_offline_dev.loader")

_MODULE_PREFIX = "astrbot_plugin_offline_dev._skills"


@dataclass(frozen=True)
class LoadResult:
    """单个技能加载结果。"""

    manifest: SkillManifest
    handler: Callable
    module: ModuleType


class SkillLoader:
    """技能扫描器 + 动态导入器。"""

    def __init__(self, skills_root: Path, blacklist: frozenset[str]) -> None:
        self._root = skills_root
        self._blacklist = blacklist

    def scan(self) -> list[Path]:
        """返回所有候选技能目录（包含 skill.yaml 的一级子目录）。"""
        if not self._root.is_dir():
            return []
        candidates: list[Path] = []
        for entry in sorted(self._root.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith(("_", ".")):
                continue
            if (entry / "skill.yaml").is_file():
                candidates.append(entry)
        return candidates

    def load_all(self) -> tuple[list[LoadResult], list[tuple[Path, str]]]:
        """加载所有技能。

        Returns:
            (成功列表, 失败列表)。失败项形如 (技能目录, 错误信息字符串)。
        """
        self._purge_old_modules()

        ok: list[LoadResult] = []
        failed: list[tuple[Path, str]] = []

        for skill_dir in self.scan():
            try:
                result = self._load_one(skill_dir)
            except Exception as e:  # noqa: BLE001
                # 单个技能失败不能中断整体，按规则记录后继续
                logger.error(
                    "[offline_dev] 加载技能失败 %s: %s", skill_dir.name, e
                )
                failed.append((skill_dir, str(e)))
                continue
            ok.append(result)
        return ok, failed

    def _load_one(self, skill_dir: Path) -> LoadResult:
        manifest = load_manifest(skill_dir)
        if manifest.name in self._blacklist:
            raise ManifestError(f"技能 {manifest.name} 在黑名单中，已跳过")

        module = self._import_handler_module(manifest)
        handler = getattr(module, manifest.entrypoint_func, None)
        if handler is None or not callable(handler):
            raise ManifestError(
                f"模块 {manifest.entrypoint_module} 中没有可调用的 "
                f"{manifest.entrypoint_func}"
            )
        return LoadResult(manifest=manifest, handler=handler, module=module)

    def _import_handler_module(self, manifest: SkillManifest) -> ModuleType:
        module_file = (
            manifest.skill_dir / f"{manifest.entrypoint_module}.py"
        ).resolve()
        # 防止 entrypoint_module 通过 .. 越界
        try:
            module_file.relative_to(manifest.skill_dir.resolve())
        except ValueError as e:
            raise ManifestError(
                f"entrypoint 指向技能目录之外: {module_file}"
            ) from e
        if not module_file.is_file():
            raise ManifestError(f"找不到入口模块文件: {module_file}")

        full_name = f"{_MODULE_PREFIX}.{manifest.name}.{manifest.entrypoint_module}"
        spec = importlib.util.spec_from_file_location(
            full_name,
            module_file,
            submodule_search_locations=[str(manifest.skill_dir)],
        )
        if spec is None or spec.loader is None:
            raise ManifestError(f"无法构建 import spec: {module_file}")

        module = importlib.util.module_from_spec(spec)
        # 先注册再 exec，让模块内部的相对导入能找到自己
        sys.modules[full_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(full_name, None)
            raise
        return module

    @staticmethod
    def _purge_old_modules() -> None:
        """重新加载前清理上一轮残留的 _skills.* 模块。"""
        stale = [k for k in sys.modules if k.startswith(_MODULE_PREFIX)]
        for key in stale:
            sys.modules.pop(key, None)
