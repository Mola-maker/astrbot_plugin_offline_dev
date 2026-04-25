"""Word Count 示例：演示带状态的技能（每次调用计数器自增）。"""

from __future__ import annotations

import json
from pathlib import Path


def _counter_path(ctx) -> Path:
    return ctx.data_dir / "calls.json"


def _read_counter(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("count", 0))
    except (OSError, ValueError):
        return 0


def _write_counter(path: Path, count: int) -> None:
    path.write_text(json.dumps({"count": count}), encoding="utf-8")


async def run(ctx) -> str:
    text = ctx.joined_args()
    if not text:
        return "wc: 请提供要统计的文本，例如  /skill run wordcount hello world"

    chars = len(text)
    words = len(text.split())

    path = _counter_path(ctx)
    count = _read_counter(path) + 1
    _write_counter(path, count)

    return (
        f"📊 统计结果\n"
        f"字符数: {chars}\n"
        f"单词数: {words}\n"
        f"本技能累计调用次数: {count}"
    )
