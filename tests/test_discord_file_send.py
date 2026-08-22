import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from app.modules.discord.discord import Discord


def test_send_file_checks_local_file_in_threadpool(monkeypatch):
    """Discord 文件发送应把本地文件检查移出 Discord 事件循环。"""
    discord_client = Discord.__new__(Discord)
    channel = AsyncMock()
    discord_client._resolve_channel = AsyncMock(return_value=channel)
    check_file = AsyncMock(return_value=False)
    monkeypatch.setattr("app.modules.discord.discord.run_in_threadpool", check_file)

    result = asyncio.run(
        discord_client._send_file(
            file_path="/tmp/missing.txt",
            title="标题",
            text=None,
            userid="user-1",
            file_name=None,
            original_chat_id=None,
        )
    )

    assert result == (False, None)
    check_file.assert_awaited_once()
    assert check_file.await_args.args[1] == Path("/tmp/missing.txt")
    channel.send.assert_not_awaited()
