"""SkillContext —— 传给技能 handler 的运行时上下文。

技能作者应只通过 SkillContext 与外部交互。这是稳定的对外 API。

互动模式 vs Loop 模式
- 互动模式：用户主动 /skill run xxx 触发，event 非空，is_loop=False。
- Loop 模式：调度器周期触发，event 为 None，is_loop=True，loop_id 给出来源。

任何想兼容 loop 模式的技能都应该：
- 用 ctx.target_session（始终有值）做路由，而不是 ctx.event.unified_msg_origin
- 在访问 ctx.event 前判 None
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.star import Context


@dataclass(frozen=True)
class SkillContext:
    """技能运行时上下文。所有字段在技能调用期间均不应被技能修改。

    Attributes:
        event: 触发本次执行的消息事件；loop 模式下为 None。
        astrbot_context: AstrBot 全局上下文，提供 Provider/会话等高级 API。
        data_dir: 本技能专属的可写持久化目录（plugin_data 下子目录）。
        config: 本技能的私有配置字典（来自 skill.yaml 的 raw 字段）。
        logger: 已带前缀的子 logger。
        args: 触发指令时附带的参数字符串列表。
        target_session: 输出应当被发送到的 session（unified_msg_origin）；
            互动模式下来自 event，loop 模式下来自 LoopSpec。
        is_loop: 是否处于 loop 模式（由调度器触发）。
        loop_id: loop 模式下的 loop id；互动模式为空串。
    """

    astrbot_context: "Context"
    data_dir: Path
    config: dict[str, Any]
    logger: Any                      # 实为 astrbot.api.logger，避免引入 logging 类型
    args: tuple[str, ...]
    target_session: str
    event: "AstrMessageEvent | None" = None
    is_loop: bool = False
    loop_id: str = ""

    def get_arg(self, index: int, default: str = "") -> str:
        """安全获取第 index 个参数，越界返回 default。"""
        if 0 <= index < len(self.args):
            return self.args[index]
        return default

    def joined_args(self, sep: str = " ") -> str:
        """把所有参数拼成一个字符串。"""
        return sep.join(self.args)
