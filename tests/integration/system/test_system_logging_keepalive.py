"""
主程序与插件日志流的空闲心跳、游标和 SSE 编码回归。
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.endpoints import system
from app.startup.composition import system as composition_system
from app.startup.composition.system import _FileLogAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "logfile",
    ["moviepilot.log", "plugins/embymetarefresh.log", "subscriptions/1.log"],
)
async def test_file_log_follow_keeps_idle_connection_without_replaying_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logfile: str,
) -> None:
    """
    三类日志在空闲轮询时都产生心跳，历史尾部和随后追加的完整行各输出一次。
    """
    path = tmp_path / logfile
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("older\nhistory\n", encoding="utf-8")
    adapter = _FileLogAdapter(SimpleNamespace(get=lambda _key: tmp_path))
    monkeypatch.setattr(
        composition_system,
        "runtime_stop_state",
        SimpleNamespace(is_system_stopped=False),
    )
    monkeypatch.setattr(composition_system.asyncio, "sleep", AsyncMock())
    polls = 0

    async def disconnected() -> bool:
        """
        第二轮保持空闲，第三轮追加一行，第四轮断开以有界消费真实文件流。
        """
        nonlocal polls
        polls += 1
        if polls == 3:
            with path.open("a", encoding="utf-8") as file:
                file.write("appended\n")
        return polls > 3

    source = await adapter.follow(logfile, 1, disconnected)
    assert [line async for line in source] == [
        "history",
        "",
        "",
        "appended",
        "",
    ]
    assert polls == 4


@pytest.mark.asyncio
async def test_file_log_follow_stops_before_reading_when_disconnected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    已断开的连接不推送历史或心跳，日志流自然结束。
    """
    (tmp_path / "moviepilot.log").write_text("history\n", encoding="utf-8")
    adapter = _FileLogAdapter(SimpleNamespace(get=lambda _key: tmp_path))
    monkeypatch.setattr(
        composition_system,
        "runtime_stop_state",
        SimpleNamespace(is_system_stopped=False),
    )
    source = await adapter.follow(
        "moviepilot.log", 50, AsyncMock(return_value=True),
    )
    assert [line async for line in source] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "logfile",
    ["moviepilot.log", "plugins/embymetarefresh.log"],
)
async def test_system_log_sse_encodes_heartbeat_as_comment(logfile: str) -> None:
    """
    心跳采用 SSE 注释且禁用缓存和代理缓冲，不向浏览器日志列表推送空 data 消息。
    """
    disconnected = AsyncMock(return_value=False)

    async def source() -> AsyncIterator[str]:
        """
        在历史行前后提供心跳，保留日志行原文。
        """
        yield ""
        yield "【INFO】历史日志"
        yield ""

    async def follow(
        path: str,
        length: int,
        disconnected_callback: Callable[[], Awaitable[bool]],
    ) -> AsyncIterator[str]:
        """
        提供遵循系统日志端口的有限迭代器，并检查请求参数沿用。
        """
        assert path == logfile
        assert length == 50
        assert disconnected_callback is disconnected
        return source()

    response = await system._get_logging_impl(
        SimpleNamespace(is_disconnected=disconnected),
        logfile=logfile,
        runtime=SimpleNamespace(system=SimpleNamespace(follow_log=follow)),
    )
    assert [chunk async for chunk in response.body_iterator] == [
        ": keepalive\n\n",
        "data: 【INFO】历史日志\n\n",
        ": keepalive\n\n",
    ]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Accel-Buffering"] == "no"
    assert response.headers["Content-Type"].startswith("text/event-stream")


@pytest.mark.asyncio
async def test_system_log_sse_cancellation_ends_without_error_event() -> None:
    """
    心跳后的客户端取消正常结束 SSE，不添加异常日志事件。
    """
    async def source() -> AsyncIterator[str]:
        """
        模拟心跳已发送后连接取消。
        """
        yield ""
        raise asyncio.CancelledError

    async def follow(*_args: object) -> AsyncIterator[str]:
        """
        返回会被取消的日志迭代器。
        """
        return source()

    response = await system._get_logging_impl(
        SimpleNamespace(is_disconnected=AsyncMock()),
        runtime=SimpleNamespace(system=SimpleNamespace(follow_log=follow)),
    )
    assert [chunk async for chunk in response.body_iterator] == [
        ": keepalive\n\n",
    ]


@pytest.mark.asyncio
async def test_system_log_full_text_does_not_include_heartbeat() -> None:
    """
    全文下载保持纯文本路径，不调用实时日志流或添加 SSE 心跳。
    """
    service = SimpleNamespace(
        read_log=AsyncMock(return_value="second\nfirst"),
        follow_log=AsyncMock(),
    )
    response = await system._get_logging_impl(
        SimpleNamespace(is_disconnected=AsyncMock()),
        length=-1,
        logfile="plugins/embymetarefresh.log",
        runtime=SimpleNamespace(system=service),
    )
    assert response.body == b"second\nfirst"
    assert response.headers["Content-Type"].startswith("text/plain")
    service.read_log.assert_awaited_once_with("plugins/embymetarefresh.log")
    service.follow_log.assert_not_awaited()
