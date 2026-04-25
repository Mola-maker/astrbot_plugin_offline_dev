"""Echo 示例技能：把输入参数原样返回。"""

from __future__ import annotations


async def run(ctx) -> str:
    """技能入口。

    Args:
        ctx: SkillContext，由框架注入。可读取 ctx.args/ctx.event/ctx.logger。

    Returns:
        要发送给用户的字符串；空字符串表示"无输出"。
    """
    if not ctx.args:
        return "echo: (没有收到任何参数)"
    ctx.logger.info("echo 技能被调用，args=%s", ctx.args)
    return f"echo: {ctx.joined_args()}"
