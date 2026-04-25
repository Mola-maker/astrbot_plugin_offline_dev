"""Loop 调度器：定时反复执行某个技能。

设计要点：
- 每个 loop 是独立的 asyncio.Task；终止时统一取消，绝不阻塞主循环。
- 每次迭代都走外部 SkillRunner，复用其超时/并发/异常隔离。
- 状态持久化到 JSON。重启后默认不自动恢复，避免突然刷屏。
- 连续失败阈值触发"断路器"自动暂停，防止失控刷消息。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import random
import secrets
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable

from astrbot.api import logger

# APScheduler 是 AstrBot 自身依赖，可直接使用其 CronTrigger 解析 cron 表达式
try:
    from apscheduler.triggers.cron import CronTrigger as _CronTrigger
except Exception:  # noqa: BLE001
    _CronTrigger = None  # type: ignore[assignment]

# 持久化文件协议版本——破坏性变更时递增
_SCHEMA_VERSION = 1

# 自动暂停的连续失败阈值
_FAILURE_BREAKER = 3


@dataclass(frozen=True)
class LoopSpec:
    """一个 loop 的可序列化规格。

    调度方式互斥：
    - cron_expr 非空 → 按 cron 表达式触发（interval_seconds 被忽略）
    - 否则按 interval_seconds 触发，叠加 ±jitter_seconds 的随机抖动
    """

    id: str
    skill_name: str
    interval_seconds: int
    args: tuple[str, ...]
    target_session: str
    enabled: bool = True
    max_iterations: int = 0  # 0 表示无限
    iterations_done: int = 0
    failure_count: int = 0
    last_error: str = ""
    created_at: float = 0.0
    last_run_at: float = 0.0
    paused_reason: str = ""
    cron_expr: str = ""
    jitter_seconds: int = 0

    @property
    def is_cron(self) -> bool:
        return bool(self.cron_expr)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["args"] = list(self.args)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LoopSpec":
        args = tuple(d.get("args", []) or [])
        return cls(
            id=str(d["id"]),
            skill_name=str(d["skill_name"]),
            interval_seconds=int(d.get("interval_seconds", 0)),
            args=args,
            target_session=str(d["target_session"]),
            enabled=bool(d.get("enabled", True)),
            max_iterations=int(d.get("max_iterations", 0)),
            iterations_done=int(d.get("iterations_done", 0)),
            failure_count=int(d.get("failure_count", 0)),
            last_error=str(d.get("last_error", "")),
            created_at=float(d.get("created_at", 0.0)),
            last_run_at=float(d.get("last_run_at", 0.0)),
            paused_reason=str(d.get("paused_reason", "")),
            cron_expr=str(d.get("cron_expr", "")),
            jitter_seconds=int(d.get("jitter_seconds", 0)),
        )


@dataclass
class LoopRecord:
    """运行时记录：spec + 当前 Task。"""

    spec: LoopSpec
    task: asyncio.Task | None = None


# 类型别名：迭代回调拿到 spec，自己负责执行技能并返回 (ok, output, error)
IterationCallback = Callable[[LoopSpec], Awaitable[tuple[bool, str, str]]]
# 投递回调：把字符串发到 target_session
DeliverCallback = Callable[[str, str], Awaitable[bool]]


class LoopScheduler:
    """管理 loop 生命周期。线程不安全，但 asyncio 单线程无所谓。"""

    def __init__(
        self,
        *,
        store_path: Path,
        min_interval_seconds: int,
        max_loops: int,
        iterate: IterationCallback,
        deliver: DeliverCallback,
    ) -> None:
        if min_interval_seconds <= 0:
            raise ValueError("min_interval_seconds 必须为正")
        if max_loops <= 0:
            raise ValueError("max_loops 必须为正")
        self._store_path = store_path
        self._min_interval = min_interval_seconds
        self._max_loops = max_loops
        self._iterate = iterate
        self._deliver = deliver

        self._records: dict[str, LoopRecord] = {}
        self._lock = asyncio.Lock()

    # ──────────────────────────── 持久化 ────────────────────────────

    def load_from_disk(self) -> list[LoopSpec]:
        """从磁盘加载 spec（不启动 Task）。失败时返回空列表，不抛。"""
        if not self._store_path.is_file():
            return []
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("[offline_dev] 读取 loops.json 失败: %s", e)
            return []
        if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
            logger.warning(
                "[offline_dev] loops.json 版本不匹配 (期望 %s)，忽略", _SCHEMA_VERSION
            )
            return []
        specs: list[LoopSpec] = []
        for raw in data.get("loops", []) or []:
            try:
                specs.append(LoopSpec.from_dict(raw))
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("[offline_dev] 跳过损坏的 loop 记录: %s", e)
        return specs

    def _persist(self) -> None:
        """快照当前所有 spec 到磁盘。同步 IO，量级很小可以接受。"""
        payload = {
            "version": _SCHEMA_VERSION,
            "loops": [r.spec.to_dict() for r in self._records.values()],
        }
        try:
            tmp = self._store_path.with_suffix(self._store_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._store_path)
        except OSError as e:
            logger.error("[offline_dev] 持久化 loops 失败: %s", e)

    # ──────────────────────────── 查询 ────────────────────────────

    def list_specs(self) -> list[LoopSpec]:
        return [r.spec for r in self._records.values()]

    def get(self, loop_id: str) -> LoopSpec | None:
        rec = self._records.get(loop_id)
        return rec.spec if rec else None

    def __len__(self) -> int:
        return len(self._records)

    # ──────────────────────────── 增删改 ────────────────────────────

    async def add(
        self,
        *,
        skill_name: str,
        args: tuple[str, ...],
        target_session: str,
        interval_seconds: int = 0,
        cron_expr: str = "",
        jitter_seconds: int = 0,
        max_iterations: int = 0,
        autostart: bool = True,
    ) -> LoopSpec:
        """注册一个新 loop。失败时抛 ValueError。

        必须指定 interval_seconds 或 cron_expr 之一（互斥）。
        """
        cron_expr = (cron_expr or "").strip()
        if cron_expr and interval_seconds:
            raise ValueError("cron_expr 与 interval_seconds 不能同时指定")
        if not cron_expr:
            if interval_seconds < self._min_interval:
                raise ValueError(
                    f"间隔不能小于 {self._min_interval} 秒（当前 {interval_seconds}）"
                )
            if jitter_seconds < 0 or jitter_seconds >= interval_seconds:
                raise ValueError(
                    f"jitter_seconds 必须满足 0 <= jitter < interval"
                )
        else:
            _validate_cron(cron_expr)
        if max_iterations < 0:
            raise ValueError("max_iterations 不能为负")
        async with self._lock:
            if len(self._records) >= self._max_loops:
                raise ValueError(
                    f"loop 数量已达上限 {self._max_loops}，请先停止部分 loop"
                )
            spec = LoopSpec(
                id=_new_id(),
                skill_name=skill_name,
                interval_seconds=interval_seconds,
                args=tuple(args),
                target_session=target_session,
                enabled=autostart,
                max_iterations=max_iterations,
                created_at=time.time(),
                cron_expr=cron_expr,
                jitter_seconds=jitter_seconds,
            )
            record = LoopRecord(spec=spec)
            self._records[spec.id] = record
            if autostart:
                record.task = asyncio.create_task(
                    self._loop_runner(spec.id), name=f"offline_dev_loop_{spec.id}"
                )
            self._persist()
            return spec

    async def upsert_template_paused(
        self,
        *,
        deterministic_id: str,
        skill_name: str,
        interval_seconds: int,
        cron_expr: str,
        jitter_seconds: int,
        args: tuple[str, ...],
    ) -> tuple[LoopSpec, bool]:
        """注册/合并一个 loop 模板（始终 paused、target_session 留空待领养）。

        Returns:
            (spec, created): created 表示是否是新建（False 即已存在则跳过）。
        """
        async with self._lock:
            existing = self._records.get(deterministic_id)
            if existing is not None:
                return existing.spec, False

            spec = LoopSpec(
                id=deterministic_id,
                skill_name=skill_name,
                interval_seconds=interval_seconds,
                args=tuple(args),
                target_session="",
                enabled=False,
                created_at=time.time(),
                cron_expr=cron_expr,
                jitter_seconds=jitter_seconds,
                paused_reason="待领养（target_session 未绑定）",
            )
            self._records[spec.id] = LoopRecord(spec=spec)
            self._persist()
            return spec, True

    async def adopt(self, loop_id: str, target_session: str) -> tuple[bool, str]:
        """把一个待领养的 loop 绑定到 target_session 并启动。

        Returns:
            (ok, message)。ok=False 时 message 是失败原因。
        """
        if not target_session:
            return False, "target_session 不能为空"
        async with self._lock:
            rec = self._records.get(loop_id)
            if rec is None:
                return False, "未找到该 loop"
            if rec.spec.target_session and rec.spec.target_session != target_session:
                return (
                    False,
                    f"该 loop 已绑定到其他会话 ({rec.spec.target_session})；"
                    f"如需改绑请先 stop 后重新创建",
                )
            rec.spec = replace(
                rec.spec,
                target_session=target_session,
                enabled=True,
                paused_reason="",
                failure_count=0,
            )
            if rec.task and not rec.task.done():
                rec.task.cancel()
                try:
                    await rec.task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            rec.task = asyncio.create_task(
                self._loop_runner(rec.spec.id),
                name=f"offline_dev_loop_{rec.spec.id}",
            )
            self._persist()
            return True, "ok"

    async def stop(self, loop_id: str) -> bool:
        """停止并删除一个 loop。返回是否找到。"""
        async with self._lock:
            rec = self._records.pop(loop_id, None)
            if rec is None:
                return False
            await self._cancel_task(rec)
            self._persist()
            return True

    async def pause(self, loop_id: str, reason: str = "manual") -> bool:
        async with self._lock:
            rec = self._records.get(loop_id)
            if rec is None or not rec.spec.enabled:
                return False
            rec.spec = replace(rec.spec, enabled=False, paused_reason=reason)
            await self._cancel_task(rec)
            self._persist()
            return True

    async def resume(self, loop_id: str) -> bool:
        async with self._lock:
            rec = self._records.get(loop_id)
            if rec is None:
                return False
            if rec.spec.enabled and rec.task and not rec.task.done():
                return True  # 已经在跑
            rec.spec = replace(
                rec.spec, enabled=True, paused_reason="", failure_count=0
            )
            rec.task = asyncio.create_task(
                self._loop_runner(rec.spec.id), name=f"offline_dev_loop_{rec.spec.id}"
            )
            self._persist()
            return True

    async def tick_once(self, loop_id: str) -> tuple[bool, str, str]:
        """手动立即触发一次迭代（不影响下一次定时调度）。

        返回 (ok, output, error)；loop 不存在返回 (False, "", "not found")。
        """
        rec = self._records.get(loop_id)
        if rec is None:
            return False, "", "not found"
        ok, output, err = await self._safe_iterate(rec.spec)
        # 不更新 last_run_at / iterations_done，避免干扰定时节奏
        if ok and output:
            await self._safe_deliver(rec.spec.target_session, output)
        return ok, output, err

    async def stop_all(self) -> int:
        # 锁内：快照所有记录、清空状态、立刻持久化、释放锁。
        # 锁外：并发取消所有 Task —— 不让"等待协程退出"在持锁期间发生，
        # 避免长时间阻塞其他 add/pause/resume 调用。
        async with self._lock:
            records_to_cancel = list(self._records.values())
            count = len(records_to_cancel)
            self._records.clear()
            self._persist()

        if records_to_cancel:
            await asyncio.gather(
                *(self._cancel_task(rec) for rec in records_to_cancel),
                return_exceptions=True,
            )
        return count

    async def restore(self, specs: list[LoopSpec], *, autostart: bool) -> int:
        """从持久化数据批量恢复 loop。返回成功启动的数量。"""
        started = 0
        async with self._lock:
            for spec in specs:
                if spec.id in self._records:
                    continue
                # 恢复时清零 failure 计数，给个新机会
                fresh = replace(
                    spec,
                    enabled=autostart and spec.enabled,
                    failure_count=0,
                    last_error="",
                )
                rec = LoopRecord(spec=fresh)
                self._records[fresh.id] = rec
                if fresh.enabled:
                    rec.task = asyncio.create_task(
                        self._loop_runner(fresh.id),
                        name=f"offline_dev_loop_{fresh.id}",
                    )
                    started += 1
            self._persist()
        return started

    # ──────────────────────────── 内部 ────────────────────────────

    @staticmethod
    async def _cancel_task(rec: LoopRecord) -> None:
        if rec.task and not rec.task.done():
            rec.task.cancel()
            try:
                await rec.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        rec.task = None

    async def _loop_runner(self, loop_id: str) -> None:
        """单个 loop 的主循环。捕获所有异常，绝不让 Task 异常退出污染日志。"""
        try:
            while True:
                rec = self._records.get(loop_id)
                if rec is None or not rec.spec.enabled:
                    return

                try:
                    delay = _compute_next_delay(rec.spec)
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise

                # 重新获取 rec（停止/暂停可能已清空）
                rec = self._records.get(loop_id)
                if rec is None or not rec.spec.enabled:
                    return

                ok, output, err = await self._safe_iterate(rec.spec)
                rec = self._records.get(loop_id)
                if rec is None:
                    return  # 期间被删除

                new_iter = rec.spec.iterations_done + 1
                if ok:
                    rec.spec = replace(
                        rec.spec,
                        iterations_done=new_iter,
                        failure_count=0,
                        last_error="",
                        last_run_at=time.time(),
                    )
                    if output:
                        await self._safe_deliver(rec.spec.target_session, output)
                else:
                    rec.spec = replace(
                        rec.spec,
                        iterations_done=new_iter,
                        failure_count=rec.spec.failure_count + 1,
                        last_error=err,
                        last_run_at=time.time(),
                    )
                    if rec.spec.failure_count >= _FAILURE_BREAKER:
                        rec.spec = replace(
                            rec.spec,
                            enabled=False,
                            paused_reason=f"连续 {_FAILURE_BREAKER} 次失败，已自动暂停",
                        )
                        logger.warning(
                            "[offline_dev] loop %s 连续失败 %d 次，自动暂停",
                            loop_id,
                            _FAILURE_BREAKER,
                        )
                        self._persist()
                        return

                # 达到上限
                if (
                    rec.spec.max_iterations > 0
                    and rec.spec.iterations_done >= rec.spec.max_iterations
                ):
                    rec.spec = replace(
                        rec.spec,
                        enabled=False,
                        paused_reason="已达到 max_iterations",
                    )
                    self._persist()
                    return

                self._persist()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[offline_dev] loop %s 主循环异常退出", loop_id)

    async def _safe_iterate(self, spec: LoopSpec) -> tuple[bool, str, str]:
        try:
            return await self._iterate(spec)
        except Exception as e:  # noqa: BLE001
            logger.exception("[offline_dev] iterate 回调抛错")
            return False, "", f"{type(e).__name__}: {e}"

    async def _safe_deliver(self, session: str, text: str) -> None:
        try:
            await self._deliver(session, text)
        except Exception as e:  # noqa: BLE001
            logger.exception("[offline_dev] deliver 失败 session=%s: %s", session, e)


def _new_id() -> str:
    """8 字符 hex，碰撞概率对本场景足够低。"""
    return secrets.token_hex(4)


def _validate_cron(expr: str) -> None:
    """校验 cron 表达式，失败抛 ValueError。"""
    if _CronTrigger is None:
        raise ValueError("apscheduler 未安装，无法解析 cron 表达式")
    try:
        _CronTrigger.from_crontab(expr)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"非法 cron 表达式 {expr!r}: {e}") from e


def _compute_next_delay(spec: LoopSpec) -> float:
    """根据 spec 计算下一次触发的等待秒数。"""
    if spec.is_cron and _CronTrigger is not None:
        try:
            trigger = _CronTrigger.from_crontab(spec.cron_expr)
            now = _dt.datetime.now().astimezone()
            next_dt = trigger.get_next_fire_time(None, now)
            if next_dt is None:
                # 没有下一次了，返回一个长睡（cron 通常都有下一次）
                return 3600.0
            delay = (next_dt - now).total_seconds()
            return max(0.5, delay)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[offline_dev] cron 计算失败 spec=%s expr=%r: %s",
                spec.id,
                spec.cron_expr,
                e,
            )
            # 退化到 60s 兜底
            return 60.0

    base = float(spec.interval_seconds)
    if spec.jitter_seconds > 0:
        # 抖动：±jitter，避免多个相同间隔的 loop 共振
        base += random.uniform(-spec.jitter_seconds, spec.jitter_seconds)
    return max(0.5, base)
