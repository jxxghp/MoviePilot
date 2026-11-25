"""
同步与异步进度帮助类的快照、更新和本地化契约。
"""

import asyncio
from unittest.mock import AsyncMock, Mock

from app.runtime.progress import AsyncProgressHelper, ProgressHelper


def test_progress_helper_lifecycle_localizes_snapshot_fields() -> None:
    """
    同步进度应支持更新、结束、附加数据和本地化字段，并安全处理空快照。
    """
    missing = ProgressHelper("test-progress-missing")
    missing._progress.get = Mock(return_value=None)
    missing.start()
    missing.end()
    missing.update()
    assert missing.get() is None

    helper = ProgressHelper("test-progress-sync")
    helper.start()
    helper.update(
        value=150,
        text="处理中",
        data={"error": "错误", "message": "消息"},
    )
    detail = helper.get(locale="zh-CN")
    assert detail is not None
    assert detail["enable"] is True
    assert detail["value"] == 100
    assert detail["text"] == "处理中"
    assert detail["data"]["error_i18n"]
    assert detail["data"]["message_i18n"]

    empty_data = ProgressHelper("test-progress-sync-end")
    empty_data.start()
    empty_data.end(data={"finished": True}, value=None)
    assert empty_data.get()["data"]["finished"] is True


def test_async_progress_helper_lifecycle_localizes_snapshot_fields() -> None:
    """
    异步进度应覆盖空快照终态、边界值更新和附加数据本地化。
    """

    async def scenario() -> None:
        """
        在独立键空间中执行异步进度的完整生命周期。
        """
        missing = AsyncProgressHelper("test-progress-async-missing")
        missing._progress.get = AsyncMock(return_value=None)
        await missing.start()
        await missing.end(data={"error": "错误"}, value=None)
        await missing.update()
        assert await missing.get() is None

        helper = AsyncProgressHelper("test-progress-async")
        await helper.start()
        await helper.update(
            value=-10,
            text="异步处理中",
            data={"error": "错误", "message": "消息"},
        )
        detail = await helper.get(locale="zh-CN")
        assert detail is not None
        assert detail["enable"] is True
        assert detail["value"] == 0
        assert detail["text"] == "异步处理中"
        assert detail["data"]["error_i18n"]
        assert detail["data"]["message_i18n"]

    asyncio.run(scenario())
