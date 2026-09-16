"""验证 Agent 多行日志在实时日志轮询中的传输完整性。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.startup.composition.system import _FileLogAdapter


@pytest.mark.asyncio
async def test_file_log_follow_drains_all_lines_from_one_append(tmp_path: Path) -> None:
    """一次写入包含多行时，实时跟随器应逐行转发全部新增内容。"""
    log_path = tmp_path / "moviepilot.log"
    log_path.touch()
    adapter = _FileLogAdapter(SimpleNamespace(get=lambda key: tmp_path))

    async def is_disconnected() -> bool:
        """保持测试连接，直到断言收集到全部新增日志。"""
        return False

    stream = await adapter.follow("moviepilot.log", 50, is_disconnected)
    collected: list[str] = []

    async def collect_lines() -> None:
        """收集同一次写入产生的三行日志。"""
        async for line in stream:
            collected.append(line)
            if len(collected) == 3:
                break

    collector = asyncio.create_task(collect_lines())
    await asyncio.sleep(0.05)
    log_path.write_text(
        "【INFO】 2026-09-11 11:39:36,300 callback - Agent消息: 首段\n"
        "...\n"
        "⚙️ => 读取技能说明：moviepilot-api\n",
        encoding="utf-8",
    )

    await asyncio.wait_for(collector, timeout=2)
    await stream.aclose()

    assert collected == [
        "【INFO】 2026-09-11 11:39:36,300 callback - Agent消息: 首段",
        "...",
        "⚙️ => 读取技能说明：moviepilot-api",
    ]
