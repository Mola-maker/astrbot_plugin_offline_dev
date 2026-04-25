"""astrbot_plugin_offline_dev —— 离线技能开发框架。

设计原则：
1. 安全：技能在 asyncio.wait_for 隔离，超时强制中断；异常永远不向上冒泡。
2. 不干扰主线程：__init__ 只登记一个后台 Task；技能执行受信号量限流；loop 调度独立 Task。
3. 可行性：技能 = 文件夹（skill.yaml + handler.py），拖进数据目录即可移植。

v0.2 新增 Loop 自动循环模式：把任意技能挂成定时任务，结果自动推送回会话。
v0.3 新增 cron 表达式 / jitter 抖动 / 手动 tick / LLM 工具 / 每技能 loop 配额。
v0.4 新增 manifest default_loops 模板（待领养） / 内嵌 Web 仪表盘 / 事件环形缓冲。
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import GreedyStr

from .core import (
    LoopScheduler,
    LoopSpec,
    LoopTemplate,
    RegisteredSkill,
    SkillContext,
    SkillLoader,
    SkillRegistry,
    SkillRunner,
)
from .web_ui import (
    EVENT_BUFFER_CAPACITY,
    EventBuffer,
    ExecutionEvent,
    OfflineDevWebUI,
)

PLUGIN_NAME = "astrbot_plugin_offline_dev"
LOG_PREFIX = "[offline_dev]"


@register(
    PLUGIN_NAME,
    "chenlihuasb",
    "离线技能开发框架：技能包热移植 + 安全异步执行 + Loop 模式（间隔/cron/模板）+ Web 仪表盘",
    "0.4.0",
    "https://github.com/chenlihuasb/astrbot_plugin_offline_dev",
)
class OfflineDevPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config

        # 关键路径
        self._data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        skills_dir_name = (
            str(self.config.get("skills_dir_name", "skills")).strip() or "skills"
        )
        self._skills_root: Path = self._data_dir / skills_dir_name
        self._skills_root.mkdir(parents=True, exist_ok=True)

        # 注册表 + 执行器
        self._registry = SkillRegistry()
        self._runner = SkillRunner(
            default_timeout=int(self.config.get("default_timeout_seconds", 30)),
            max_concurrent=int(self.config.get("max_concurrent_skills", 4)),
        )
        self._loader_lock = asyncio.Lock()

        # Loop 调度器
        self._scheduler = LoopScheduler(
            store_path=self._data_dir / "loops.json",
            min_interval_seconds=int(
                self.config.get("loop_min_interval_seconds", 5)
            ),
            max_loops=int(self.config.get("loop_max_count", 10)),
            iterate=self._loop_iterate,
            deliver=self._loop_deliver,
        )

        # 执行事件环形缓冲，喂给 Web UI 实时显示
        self._events = EventBuffer(EVENT_BUFFER_CAPACITY)

        # WebUI 注册（按配置可关闭）
        self._web_ui: OfflineDevWebUI | None = None
        if bool(self.config.get("enable_web_ui", True)):
            try:
                self._web_ui = OfflineDevWebUI(
                    astrbot_context=self.context,
                    registry=self._registry,
                    scheduler=self._scheduler,
                    events=self._events,
                    on_pause=self._scheduler.pause,
                    on_resume=self._scheduler.resume,
                    on_stop=self._scheduler.stop,
                    on_tick=self._wrapped_tick_for_ui,
                )
                self._web_ui.register()
            except Exception:  # noqa: BLE001
                logger.exception(f"{LOG_PREFIX} 注册 WebUI 失败，已退化为仅指令模式")

        # __init__ 不阻塞 —— 真正的扫描/导入丢到后台 Task
        asyncio.create_task(self._initialize_async())
        logger.info(f"{LOG_PREFIX} 插件已加载，技能根目录: {self._skills_root}")

    # ──────────────────────────────────────────────────────────────
    # 初始化与重新加载
    # ──────────────────────────────────────────────────────────────

    async def _initialize_async(self) -> None:
        """后台初始化：装示例（可选） + 扫描技能 + 恢复 loop + 注册模板。"""
        try:
            if self.config.get("auto_install_examples", True):
                self._install_examples_if_empty()
            await self._reload_skills()

            # Loop 恢复：只读 spec，按配置决定要不要自动启动
            specs = self._scheduler.load_from_disk()
            if specs:
                auto = bool(self.config.get("loop_auto_resume_on_start", False))
                started = await self._scheduler.restore(specs, autostart=auto)
                logger.info(
                    f"{LOG_PREFIX} 从磁盘恢复 {len(specs)} 个 loop，"
                    f"已启动 {started} 个 (auto_resume={auto})"
                )

            # 注册 manifest 内嵌的 default_loops 模板（始终 paused）
            tpl_count = await self._register_default_loop_templates()
            if tpl_count:
                logger.info(
                    f"{LOG_PREFIX} 已注册 {tpl_count} 个 loop 模板 "
                    f"(/skill loop adopt <id> 可领养)"
                )
        except Exception as e:  # noqa: BLE001
            # 初始化失败不能让 AstrBot 跟着一起挂
            logger.exception(f"{LOG_PREFIX} 后台初始化失败: {e}")

    async def _register_default_loop_templates(self) -> int:
        """把所有技能的 default_loops 注册为待领养模板。返回新增数。"""
        new_count = 0
        for skill in self._registry:
            for tpl in skill.manifest.default_loops:
                deterministic_id = f"tpl_{skill.manifest.name}_{tpl.id_suffix}"
                try:
                    _spec, created = await self._scheduler.upsert_template_paused(
                        deterministic_id=deterministic_id,
                        skill_name=skill.manifest.name,
                        interval_seconds=tpl.interval_seconds,
                        cron_expr=tpl.cron_expr,
                        jitter_seconds=tpl.jitter_seconds,
                        args=tpl.args,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        f"{LOG_PREFIX} 注册模板失败 {deterministic_id}"
                    )
                    continue
                if created:
                    new_count += 1
        return new_count

    def _install_examples_if_empty(self) -> None:
        """技能目录为空时，把插件自带的 examples 复制过去。"""
        if any(self._skills_root.iterdir()):
            return
        examples_src = Path(__file__).resolve().parent / "examples"
        if not examples_src.is_dir():
            return
        for entry in examples_src.iterdir():
            if not entry.is_dir():
                continue
            dest = self._skills_root / entry.name
            try:
                shutil.copytree(entry, dest)
                logger.info(f"{LOG_PREFIX} 已安装示例技能: {entry.name}")
            except OSError as e:
                logger.warning(
                    f"{LOG_PREFIX} 安装示例 {entry.name} 失败: {e}"
                )

    async def _reload_skills(self) -> tuple[int, int]:
        """扫描并重新加载所有技能。返回 (成功数, 失败数)。"""
        async with self._loader_lock:
            blacklist = frozenset(self.config.get("skill_blacklist", []) or [])
            loader = SkillLoader(self._skills_root, blacklist)
            ok, failed = await asyncio.to_thread(loader.load_all)

            registered: list[RegisteredSkill] = []
            seen_names: set[str] = set()
            for result in ok:
                name = result.manifest.name
                if name in seen_names:
                    logger.warning(
                        f"{LOG_PREFIX} 跳过重名技能: {name} "
                        f"({result.manifest.skill_dir})"
                    )
                    continue
                seen_names.add(name)
                skill_data_dir = self._data_dir / "skill_data" / name
                skill_data_dir.mkdir(parents=True, exist_ok=True)
                registered.append(
                    RegisteredSkill(
                        manifest=result.manifest,
                        handler=result.handler,
                        data_dir=skill_data_dir,
                    )
                )

            self._registry.replace_all(registered)
            logger.info(
                f"{LOG_PREFIX} 加载完成：成功 {len(registered)} 个，失败 {len(failed)} 个"
            )

        # 注册可能新增的 default_loops 模板（已存在的会跳过，不会重复注册）
        try:
            await self._register_default_loop_templates()
        except Exception:  # noqa: BLE001
            logger.exception(f"{LOG_PREFIX} reload 后注册模板失败")
        return len(registered), len(failed)

    # ──────────────────────────────────────────────────────────────
    # 公共：构造 SkillContext + 跑技能
    # ──────────────────────────────────────────────────────────────

    def _build_ctx(
        self,
        *,
        skill: RegisteredSkill,
        event: AstrMessageEvent | None,
        target_session: str,
        args: tuple[str, ...],
        is_loop: bool = False,
        loop_id: str = "",
    ) -> SkillContext:
        return SkillContext(
            astrbot_context=self.context,
            data_dir=skill.data_dir,
            config=dict(skill.manifest.raw),
            logger=logging.getLogger(f"offline_dev.skill.{skill.name}"),
            args=args,
            target_session=target_session,
            event=event,
            is_loop=is_loop,
            loop_id=loop_id,
        )

    # ──────────────────────────────────────────────────────────────
    # /skill ...
    # ──────────────────────────────────────────────────────────────

    @filter.command_group("skill")
    async def skill_group(self):
        """技能管理指令组。"""
        pass

    @skill_group.command("list")
    async def cmd_list(self, event: AstrMessageEvent):
        """列出所有已加载技能。用法: /skill list"""
        if len(self._registry) == 0:
            yield event.plain_result("📋 当前未加载任何技能")
            return
        lines = ["📋 已加载技能:", "=" * 30]
        for i, skill in enumerate(self._registry, 1):
            m = skill.manifest
            label = m.display_name or m.name
            lines.append(
                f"{i}. /{m.trigger}  —  {label} v{m.version}"
                + (f"  ({m.description})" if m.description else "")
            )
        lines.append("=" * 30)
        lines.append(f"共 {len(self._registry)} 个技能")
        yield event.plain_result("\n".join(lines))

    @skill_group.command("info")
    async def cmd_info(self, event: AstrMessageEvent, name: str):
        """查看技能详情。用法: /skill info <name>"""
        skill = self._registry.get(name)
        if skill is None:
            yield event.plain_result(f"❌ 未找到技能: {name}")
            return
        m = skill.manifest
        yield event.plain_result(
            "\n".join(
                [
                    f"📦 {m.display_name or m.name}",
                    f"name: {m.name}",
                    f"trigger: /{m.trigger}",
                    f"version: {m.version}",
                    f"author: {m.author or '(未填)'}",
                    f"permission: {m.permission}",
                    f"timeout: {m.timeout_seconds or '默认'}",
                    f"path: {m.skill_dir}",
                    f"desc: {m.description or '(无)'}",
                ]
            )
        )

    @skill_group.command("run")
    async def cmd_run(
        self,
        event: AstrMessageEvent,
        name: str,
        rest: GreedyStr = GreedyStr(""),
    ):
        """运行一个技能。用法: /skill run <name> [args...]"""
        skill = self._registry.get(name)
        if skill is None:
            yield event.plain_result(f"❌ 未找到技能: {name}")
            return
        if skill.manifest.permission == "admin" and not _is_admin(event):
            yield event.plain_result("⛔ 该技能仅管理员可执行")
            return

        args = tuple(_safe_split(str(rest)))
        ctx = self._build_ctx(
            skill=skill,
            event=event,
            target_session=event.unified_msg_origin,
            args=args,
        )
        result = await self._runner.run(skill, ctx)
        self._record_event(
            kind="manual",
            skill_name=skill.name,
            loop_id="",
            ok=result.ok,
            output=result.output,
            error=result.error,
            duration_ms=result.duration_ms,
        )
        if result.ok:
            body = result.output or "(技能未返回输出)"
            yield event.plain_result(
                f"✅ {skill.name} ({result.duration_ms}ms)\n{body}"
            )
        else:
            yield event.plain_result(
                f"❌ {skill.name} 执行失败: {result.error}"
            )

    @skill_group.command("reload")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_reload(self, event: AstrMessageEvent):
        """重新扫描并加载所有技能（管理员）。用法: /skill reload"""
        if not self.config.get("enable_auto_reload_command", True):
            yield event.plain_result("⚠️ /skill reload 已被配置禁用")
            return
        ok_count, fail_count = await self._reload_skills()
        yield event.plain_result(
            f"🔄 重新加载完成：成功 {ok_count} 个，失败 {fail_count} 个"
        )

    @skill_group.command("path")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_path(self, event: AstrMessageEvent):
        """查看技能根目录绝对路径（管理员）。"""
        yield event.plain_result(f"📁 {self._skills_root}")

    # ──────────────────────────────────────────────────────────────
    # Loop 挂载前置校验（共用）
    # ──────────────────────────────────────────────────────────────

    def _validate_loop_attach(
        self,
        skill: RegisteredSkill | None,
        skill_name: str,
        is_admin_caller: bool,
    ) -> str:
        """返回错误消息字符串；通过则返回空串。"""
        if skill is None:
            return f"未找到技能: {skill_name}"
        if skill.manifest.permission == "admin" and not is_admin_caller:
            return "该技能仅管理员可挂 loop"
        if not skill.manifest.loopable:
            return f"技能 {skill_name} 在 manifest 中声明 loopable=false，禁止挂 loop"
        cap = skill.manifest.max_loop_instances
        if cap > 0:
            existing = sum(
                1 for s in self._scheduler.list_specs() if s.skill_name == skill_name
            )
            if existing >= cap:
                return (
                    f"技能 {skill_name} 已有 {existing} 个 loop，"
                    f"达到 manifest 配额 {cap}"
                )
        return ""

    # ──────────────────────────────────────────────────────────────
    # /skill loop ...
    # ──────────────────────────────────────────────────────────────

    @skill_group.group("loop")
    def loop_group(self):
        """Loop 自动循环模式。"""
        pass

    @loop_group.command("add")
    async def cmd_loop_add(
        self,
        event: AstrMessageEvent,
        name: str,
        interval: int,
        rest: GreedyStr = GreedyStr(""),
    ):
        """新增定时 loop。用法: /skill loop add <name> <间隔秒> [args...]"""
        if not self._loop_command_enabled():
            yield event.plain_result("⚠️ /skill loop 指令组已被配置禁用")
            return

        skill = self._registry.get(name)
        if msg := self._validate_loop_attach(skill, name, _is_admin(event)):
            yield event.plain_result(f"❌ {msg}")
            return

        jitter = int(self.config.get("loop_default_jitter_seconds", 0))
        args = tuple(_safe_split(str(rest)))
        try:
            spec = await self._scheduler.add(
                skill_name=name,
                interval_seconds=interval,
                jitter_seconds=jitter if jitter < interval else 0,
                args=args,
                target_session=event.unified_msg_origin,
                autostart=True,
            )
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
            return

        yield event.plain_result(
            f"✅ 已挂载 loop\n"
            f"id: {spec.id}\n"
            f"skill: /{spec.skill_name}\n"
            f"interval: {spec.interval_seconds}s"
            + (f" (±{spec.jitter_seconds}s 抖动)" if spec.jitter_seconds else "")
            + f"\nargs: {' '.join(spec.args) or '(无)'}\n"
            f"管理: /skill loop list | stop {spec.id} | pause {spec.id}"
        )

    @loop_group.command("addcron")
    async def cmd_loop_addcron(
        self,
        event: AstrMessageEvent,
        name: str,
        cron: str,
        rest: GreedyStr = GreedyStr(""),
    ):
        """按 cron 表达式挂 loop。用法: /skill loop addcron <name> "<cron>" [args...]

        cron 表达式 5 字段：分 时 日 月 周，例 "0 9 * * *" 每天 9:00。
        含空格请用引号包起来；不含空格可省略引号。
        """
        if not self._loop_command_enabled():
            yield event.plain_result("⚠️ /skill loop 指令组已被配置禁用")
            return

        skill = self._registry.get(name)
        if msg := self._validate_loop_attach(skill, name, _is_admin(event)):
            yield event.plain_result(f"❌ {msg}")
            return

        args = tuple(_safe_split(str(rest)))
        try:
            spec = await self._scheduler.add(
                skill_name=name,
                cron_expr=cron,
                args=args,
                target_session=event.unified_msg_origin,
                autostart=True,
            )
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
            return

        yield event.plain_result(
            f"✅ 已挂载 cron loop\n"
            f"id: {spec.id}\n"
            f"skill: /{spec.skill_name}\n"
            f"cron: {spec.cron_expr}\n"
            f"args: {' '.join(spec.args) or '(无)'}\n"
            f"管理: /skill loop list | stop {spec.id}"
        )

    @loop_group.command("tick")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_loop_tick(self, event: AstrMessageEvent, loop_id: str):
        """立即手动触发一次（不影响下一次定时）。用法: /skill loop tick <id>"""
        ok, output, err = await self._scheduler.tick_once(loop_id)
        if err == "not found":
            yield event.plain_result(f"❌ 未找到 loop: {loop_id}")
            return
        # 写事件
        spec = self._scheduler.get(loop_id)
        if spec:
            self._record_event(
                kind="tick",
                skill_name=spec.skill_name,
                loop_id=loop_id,
                ok=ok,
                output=output,
                error=err,
                duration_ms=0,
            )
        if ok:
            yield event.plain_result(
                f"⚡ tick 完成\n{output or '(无输出，已按持久化设置投递)'}"
            )
        else:
            yield event.plain_result(f"❌ tick 失败: {err}")

    @loop_group.command("adopt")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_loop_adopt(self, event: AstrMessageEvent, loop_id: str):
        """领养一个待领养模板：绑定到当前会话并启动（管理员）。

        用法（在目标群/会话里发）：/skill loop adopt <模板id>
        模板由技能 manifest 的 default_loops 自动产生，id 形如 tpl_<skill>_<suffix>。
        """
        ok, msg = await self._scheduler.adopt(loop_id, event.unified_msg_origin)
        if not ok:
            yield event.plain_result(f"❌ 领养失败: {msg}")
            return
        spec = self._scheduler.get(loop_id)
        yield event.plain_result(
            f"✅ 已领养 loop {loop_id}\n"
            f"绑定会话: {event.unified_msg_origin}\n"
            f"调度: " + (
                f"cron {spec.cron_expr}"
                if spec and spec.is_cron
                else f"every {spec.interval_seconds}s" if spec else "?"
            )
        )

    @loop_group.command("list")
    async def cmd_loop_list(self, event: AstrMessageEvent):
        """列出所有 loop。用法: /skill loop list"""
        specs = self._scheduler.list_specs()
        if not specs:
            yield event.plain_result("📋 当前没有 loop")
            return
        lines = ["🔁 Loop 列表:", "=" * 30]
        for s in specs:
            status = "▶ running" if s.enabled else f"⏸ paused ({s.paused_reason or 'manual'})"
            schedule = (
                f"cron {s.cron_expr!r}"
                if s.is_cron
                else f"every {s.interval_seconds}s"
                + (f" ±{s.jitter_seconds}s" if s.jitter_seconds else "")
            )
            lines.append(
                f"[{s.id}] /{s.skill_name} {schedule}  "
                f"iter={s.iterations_done}"
                + (f"/{s.max_iterations}" if s.max_iterations else "")
                + f"  fail={s.failure_count}  {status}"
            )
        lines.append("=" * 30)
        lines.append(f"共 {len(specs)} 个")
        yield event.plain_result("\n".join(lines))

    @loop_group.command("info")
    async def cmd_loop_info(self, event: AstrMessageEvent, loop_id: str):
        """查看某个 loop 详情。用法: /skill loop info <id>"""
        spec = self._scheduler.get(loop_id)
        if spec is None:
            yield event.plain_result(f"❌ 未找到 loop: {loop_id}")
            return
        schedule = (
            f"cron: {spec.cron_expr}"
            if spec.is_cron
            else f"interval: {spec.interval_seconds}s"
            + (f"  jitter: ±{spec.jitter_seconds}s" if spec.jitter_seconds else "")
        )
        yield event.plain_result(
            "\n".join(
                [
                    f"🔁 loop {spec.id}",
                    f"skill: /{spec.skill_name}",
                    schedule,
                    f"args: {' '.join(spec.args) or '(无)'}",
                    f"target: {spec.target_session}",
                    f"enabled: {spec.enabled}",
                    f"iterations: {spec.iterations_done}"
                    + (f" / {spec.max_iterations}" if spec.max_iterations else ""),
                    f"failure_count: {spec.failure_count}",
                    f"last_error: {spec.last_error or '(无)'}",
                    f"paused_reason: {spec.paused_reason or '(无)'}",
                ]
            )
        )

    @loop_group.command("stop")
    async def cmd_loop_stop(self, event: AstrMessageEvent, loop_id: str):
        """停止并删除一个 loop。用法: /skill loop stop <id>"""
        if await self._scheduler.stop(loop_id):
            yield event.plain_result(f"🗑️ 已删除 loop {loop_id}")
        else:
            yield event.plain_result(f"❌ 未找到 loop: {loop_id}")

    @loop_group.command("pause")
    async def cmd_loop_pause(self, event: AstrMessageEvent, loop_id: str):
        """暂停一个 loop（保留记录）。用法: /skill loop pause <id>"""
        if await self._scheduler.pause(loop_id, reason="manual"):
            yield event.plain_result(f"⏸️ loop {loop_id} 已暂停")
        else:
            yield event.plain_result(f"❌ loop 不存在或未在运行: {loop_id}")

    @loop_group.command("resume")
    async def cmd_loop_resume(self, event: AstrMessageEvent, loop_id: str):
        """恢复一个 loop。用法: /skill loop resume <id>"""
        if await self._scheduler.resume(loop_id):
            yield event.plain_result(f"▶️ loop {loop_id} 已恢复")
        else:
            yield event.plain_result(f"❌ 未找到 loop: {loop_id}")

    @loop_group.command("clear")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_loop_clear(self, event: AstrMessageEvent):
        """删除所有 loop（管理员）。用法: /skill loop clear"""
        count = await self._scheduler.stop_all()
        yield event.plain_result(f"🧹 已清空 {count} 个 loop")

    # ──────────────────────────────────────────────────────────────
    # 调度器回调
    # ──────────────────────────────────────────────────────────────

    async def _loop_iterate(self, spec: LoopSpec) -> tuple[bool, str, str]:
        """调度器的迭代回调：根据 spec 执行一次技能。"""
        skill = self._registry.get(spec.skill_name)
        if skill is None:
            err = f"技能 {spec.skill_name} 不存在或已被卸载"
            self._record_event(
                kind="loop", skill_name=spec.skill_name, loop_id=spec.id,
                ok=False, output="", error=err, duration_ms=0,
            )
            return False, "", err

        ctx = self._build_ctx(
            skill=skill,
            event=None,
            target_session=spec.target_session,
            args=spec.args,
            is_loop=True,
            loop_id=spec.id,
        )
        result = await self._runner.run(skill, ctx)
        self._record_event(
            kind="loop",
            skill_name=spec.skill_name,
            loop_id=spec.id,
            ok=result.ok,
            output=result.output,
            error=result.error,
            duration_ms=result.duration_ms,
        )
        if result.ok:
            return True, result.output, ""
        # 是否把失败也推回会话
        if self.config.get("loop_send_failure_message", False):
            return True, f"⚠️ loop {spec.id} 执行失败: {result.error}", ""
        return False, "", result.error

    async def _wrapped_tick_for_ui(self, loop_id: str):
        """WebUI 调用的 tick：执行 + 写事件。"""
        ok, output, err = await self._scheduler.tick_once(loop_id)
        if err != "not found":
            spec = self._scheduler.get(loop_id)
            if spec:
                self._record_event(
                    kind="tick",
                    skill_name=spec.skill_name,
                    loop_id=loop_id,
                    ok=ok,
                    output=output,
                    error=err,
                    duration_ms=0,
                )
        return ok, output, err

    def _record_event(
        self,
        *,
        kind: str,
        skill_name: str,
        loop_id: str,
        ok: bool,
        output: str,
        error: str,
        duration_ms: int,
    ) -> None:
        """轻量事件录入；任何失败都不应影响主流程。"""
        try:
            preview = (output or "")[:120]
            self._events.push(
                ExecutionEvent(
                    ts=time.time(),
                    kind=kind,
                    skill_name=skill_name,
                    loop_id=loop_id,
                    ok=ok,
                    duration_ms=duration_ms,
                    output_preview=preview,
                    error=error or "",
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"{LOG_PREFIX} 写事件缓冲失败")

    async def _loop_deliver(self, session: str, text: str) -> bool:
        """调度器的投递回调：把文本送回 target_session。"""
        if not text:
            return True
        chain = MessageChain().message(text)
        return await self.context.send_message(session, chain)

    # ──────────────────────────────────────────────────────────────
    # LLM 工具（默认关闭，需要在配置里开 enable_llm_tools）
    # ──────────────────────────────────────────────────────────────

    @filter.llm_tool(name="run_offline_skill")
    async def llm_run_skill(
        self,
        event: AstrMessageEvent,
        name: str,
        args: str = "",
    ) -> str:
        """调用一个本地离线技能，返回技能输出。

        仅当用户明确请求执行某个本地工具/技能时使用。绝不要为了搜索网络或回答常识性问题而调用。

        Args:
            name(string): 要执行的技能名（与 /skill list 里看到的一致）
            args(string): 传给技能的参数，按空格分割；没有可传空串
        """
        if not self.config.get("enable_llm_tools", False):
            return "拒绝：本插件 LLM 工具开关未启用"
        skill = self._registry.get(name)
        if skill is None:
            return f"未找到技能: {name}"
        if skill.manifest.permission == "admin" and not _is_admin(event):
            return f"技能 {name} 仅管理员可执行"

        ctx = self._build_ctx(
            skill=skill,
            event=event,
            target_session=event.unified_msg_origin,
            args=tuple(_safe_split(args)),
        )
        result = await self._runner.run(skill, ctx)
        if not result.ok:
            return f"技能 {name} 执行失败: {result.error}"
        return result.output or f"(技能 {name} 已执行，未返回内容)"

    @filter.llm_tool(name="schedule_offline_skill_loop")
    async def llm_schedule_loop(
        self,
        event: AstrMessageEvent,
        name: str,
        interval_seconds: int,
        args: str = "",
    ) -> str:
        """把一个本地技能挂成定时任务，结果会自动发回当前会话。

        仅当用户明确要求"定时/循环/每隔多少秒/每多久跑一次"时使用。
        创建成功后返回的 loop id 可用于 /skill loop stop 等操作。

        Args:
            name(string): 要循环执行的技能名
            interval_seconds(number): 间隔秒数；不能小于配置的最小间隔
            args(string): 每次执行传给技能的参数（空格分割），没有可传空串
        """
        if not self.config.get("enable_llm_tools", False):
            return "拒绝：本插件 LLM 工具开关未启用"
        skill = self._registry.get(name)
        if msg := self._validate_loop_attach(skill, name, _is_admin(event)):
            return f"拒绝：{msg}"

        try:
            spec = await self._scheduler.add(
                skill_name=name,
                interval_seconds=int(interval_seconds),
                args=tuple(_safe_split(args)),
                target_session=event.unified_msg_origin,
                autostart=True,
            )
        except ValueError as e:
            return f"创建 loop 失败：{e}"
        return (
            f"已创建 loop id={spec.id}，每 {spec.interval_seconds}s 跑一次 "
            f"/{spec.skill_name}。停止用 /skill loop stop {spec.id}"
        )

    # ──────────────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────────────

    async def terminate(self) -> None:
        """插件卸载。停掉所有 loop，清空注册表。"""
        try:
            await self._scheduler.stop_all()
        except Exception:  # noqa: BLE001
            logger.exception(f"{LOG_PREFIX} 关停 loop 异常")
        self._registry.replace_all([])
        logger.info(f"{LOG_PREFIX} 插件已卸载")

    # ──────────────────────────────────────────────────────────────
    # 工具
    # ──────────────────────────────────────────────────────────────

    def _loop_command_enabled(self) -> bool:
        return bool(self.config.get("enable_loop_command", True))


def _safe_split(text: str) -> list[str]:
    """用 shlex 解析参数；失败时退回 split()，绝不抛错。"""
    text = (text or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _is_admin(event: AstrMessageEvent) -> bool:
    """尽量复用 AstrBot 自身的管理员判断；判断失败时保守地返回 False。"""
    try:
        role = getattr(event, "role", None)
        if isinstance(role, str) and role.lower() == "admin":
            return True
    except Exception:  # noqa: BLE001
        pass
    return False
