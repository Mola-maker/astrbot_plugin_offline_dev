"""安全的异步技能执行器。

三大原则的落地：
- **安全**：超时强制取消、异常全捕获、永远不让技能异常向上冒泡到主事件循环。
- **不干扰主线程**：每次执行都是独立 Task；并发受 Semaphore 限流。
- **可行性**：调用契约简单——`async handler(ctx, *args) -> str`，同步函数也兼容
  （会被 `asyncio.to_thread` 包一层）。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass

from astrbot.api import logger

from .context import SkillContext
from .registry import RegisteredSkill


@dataclass(frozen=True)
class SkillExecution:
    """一次技能执行的结果记录。"""

    name: str
    ok: bool
    output: str
    duration_ms: int
    error: str = ""


class SkillRunner:
    """技能执行器。负责并发限流、超时与异常隔离。"""

    def __init__(
        self, *, default_timeout: int, max_concurrent: int
    ) -> None:
        if default_timeout <= 0:
            raise ValueError("default_timeout 必须为正")
        if max_concurrent <= 0:
            raise ValueError("max_concurrent 必须为正")
        self._default_timeout = default_timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def run(
        self, skill: RegisteredSkill, ctx: SkillContext
    ) -> SkillExecution:
        """执行技能，绝不抛出。所有异常都被打包成失败结果。"""
        timeout = skill.manifest.timeout_seconds or self._default_timeout

        # 拒绝超额并发：先看一眼信号量，满了直接返回（best-effort，
        # check-then-acquire 之间存在轻微竞争，但仅会让短暂超额一两个槽位通过）
        if self._semaphore.locked():
            return SkillExecution(
                name=skill.name,
                ok=False,
                output="",
                duration_ms=0,
                error="并发已达上限，请稍后再试",
            )

        start = time.monotonic()
        async with self._semaphore:
            try:
                output = await asyncio.wait_for(
                    self._invoke(skill, ctx), timeout=timeout
                )
            except asyncio.TimeoutError:
                duration = int((time.monotonic() - start) * 1000)
                logger.warning(
                    "[offline_dev] 技能 %s 执行超时 (%ss)",
                    skill.name,
                    timeout,
                )
                return SkillExecution(
                    name=skill.name,
                    ok=False,
                    output="",
                    duration_ms=duration,
                    error=f"执行超时（>{timeout}s）",
                )
            except Exception as e:  # noqa: BLE001
                duration = int((time.monotonic() - start) * 1000)
                logger.exception("[offline_dev] 技能 %s 抛出异常", skill.name)
                return SkillExecution(
                    name=skill.name,
                    ok=False,
                    output="",
                    duration_ms=duration,
                    error=f"{type(e).__name__}: {e}",
                )

        duration = int((time.monotonic() - start) * 1000)
        return SkillExecution(
            name=skill.name,
            ok=True,
            output=_coerce_output(output),
            duration_ms=duration,
        )

    @staticmethod
    async def _invoke(skill: RegisteredSkill, ctx: SkillContext):
        """根据 handler 是否是协程，决定怎么调。"""
        handler = skill.handler
        if inspect.iscoroutinefunction(handler):
            return await handler(ctx)
        # 同步函数：丢线程池，避免阻塞事件循环
        return await asyncio.to_thread(handler, ctx)


def _coerce_output(value: object) -> str:
    """把 handler 返回值规整为字符串；None/空返回空串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
