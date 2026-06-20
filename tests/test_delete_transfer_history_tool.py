import asyncio
from types import SimpleNamespace

from app.agent.tools.impl.delete_transfer_history import DeleteTransferHistoryTool
from app.agent.prompt.transfer_redo import build_manual_redo_template_context


def test_delete_transfer_history_tool_removes_old_dest_file_before_history(monkeypatch):
    """AI 重新整理删除整理记录前，应按历史目标文件清理旧媒体库文件。"""
    calls = []
    history = SimpleNamespace(
        id=7,
        title="奔跑吧",
        src="/downloads/Keep.Running.mkv",
        status=True,
        mode="link",
        dest_fileitem={
            "storage": "local",
            "path": "/library/奔跑吧 (2014)/Keep.Running.mkv",
            "name": "Keep.Running.mkv",
            "type": "file",
        },
    )

    class FakeTransferHistoryOper:
        async def async_get(self, history_id):
            calls.append(("get", history_id))
            return history

        async def async_delete(self, history_id):
            calls.append(("delete_history", history_id))

    class FakeStorageChain:
        def exists(self, fileitem):
            calls.append(("exists_dest", fileitem.path))
            return True

        def delete_media_file(self, fileitem):
            calls.append(("delete_dest", fileitem.path))
            return True

    monkeypatch.setattr(
        "app.agent.tools.impl.delete_transfer_history.TransferHistoryOper",
        FakeTransferHistoryOper,
    )
    monkeypatch.setattr(
        "app.agent.tools.impl.delete_transfer_history.StorageChain",
        FakeStorageChain,
    )

    tool = DeleteTransferHistoryTool(session_id="redo-session", user_id="10001")
    result = asyncio.run(tool.run(history_id=7))

    assert "已删除整理历史记录" in result
    assert calls == [
        ("get", 7),
        ("exists_dest", "/library/奔跑吧 (2014)/Keep.Running.mkv"),
        ("delete_dest", "/library/奔跑吧 (2014)/Keep.Running.mkv"),
        ("delete_history", 7),
    ]


def test_delete_transfer_history_tool_keeps_history_when_old_dest_delete_fails(monkeypatch):
    """旧媒体库文件删除失败时不得删除整理记录，避免重整链路丢失回滚依据。"""
    calls = []
    history = SimpleNamespace(
        id=8,
        title="奔跑吧",
        src="/downloads/Keep.Running.mkv",
        status=True,
        mode="copy",
        dest_fileitem={
            "storage": "local",
            "path": "/library/奔跑吧 (2014)/Keep.Running.mkv",
            "name": "Keep.Running.mkv",
            "type": "file",
        },
    )

    class FakeTransferHistoryOper:
        async def async_get(self, history_id):
            calls.append(("get", history_id))
            return history

        async def async_delete(self, history_id):
            calls.append(("delete_history", history_id))

    class FakeStorageChain:
        def exists(self, fileitem):
            calls.append(("exists_dest", fileitem.path))
            return True

        def delete_media_file(self, fileitem):
            calls.append(("delete_dest", fileitem.path))
            return False

    monkeypatch.setattr(
        "app.agent.tools.impl.delete_transfer_history.TransferHistoryOper",
        FakeTransferHistoryOper,
    )
    monkeypatch.setattr(
        "app.agent.tools.impl.delete_transfer_history.StorageChain",
        FakeStorageChain,
    )

    tool = DeleteTransferHistoryTool(session_id="redo-session", user_id="10001")
    result = asyncio.run(tool.run(history_id=8))

    assert "旧媒体库文件删除失败" in result
    assert calls == [
        ("get", 8),
        ("exists_dest", "/library/奔跑吧 (2014)/Keep.Running.mkv"),
        ("delete_dest", "/library/奔跑吧 (2014)/Keep.Running.mkv"),
    ]


def test_delete_transfer_history_tool_deletes_history_when_old_dest_is_missing(monkeypatch):
    """旧媒体库文件已不存在时应视为已清理，继续删除整理记录。"""
    calls = []
    history = SimpleNamespace(
        id=13,
        title="奔跑吧",
        src="/downloads/Keep.Running.mkv",
        status=True,
        mode="link",
        dest_fileitem={
            "storage": "local",
            "path": "/library/奔跑吧 (2014)/Keep.Running.mkv",
            "name": "Keep.Running.mkv",
            "type": "file",
        },
    )

    class FakeTransferHistoryOper:
        async def async_get(self, history_id):
            calls.append(("get", history_id))
            return history

        async def async_delete(self, history_id):
            calls.append(("delete_history", history_id))

    class FakeStorageChain:
        def exists(self, fileitem):
            calls.append(("exists_dest", fileitem.path))
            return False

        def delete_media_file(self, fileitem):
            calls.append(("delete_dest", fileitem.path))
            return False

    monkeypatch.setattr(
        "app.agent.tools.impl.delete_transfer_history.TransferHistoryOper",
        FakeTransferHistoryOper,
    )
    monkeypatch.setattr(
        "app.agent.tools.impl.delete_transfer_history.StorageChain",
        FakeStorageChain,
    )

    tool = DeleteTransferHistoryTool(session_id="redo-session", user_id="10001")
    result = asyncio.run(tool.run(history_id=13))

    assert "已删除整理历史记录" in result
    assert "已删除旧媒体库文件" not in result
    assert calls == [
        ("get", 13),
        ("exists_dest", "/library/奔跑吧 (2014)/Keep.Running.mkv"),
        ("delete_history", 13),
    ]


def test_delete_transfer_history_tool_keeps_successful_move_dest_as_reorganize_source(monkeypatch):
    """成功 move 记录的目标文件是重新整理输入，不应在删除历史时先删除。"""
    calls = []
    history = SimpleNamespace(
        id=9,
        title="奔跑吧",
        src="/downloads/Keep.Running.mkv",
        status=True,
        mode="move",
        dest_fileitem={
            "storage": "local",
            "path": "/library/奔跑吧 (2014)/Keep.Running.mkv",
            "name": "Keep.Running.mkv",
            "type": "file",
        },
    )

    class FakeTransferHistoryOper:
        async def async_get(self, history_id):
            calls.append(("get", history_id))
            return history

        async def async_delete(self, history_id):
            calls.append(("delete_history", history_id))

    class FakeStorageChain:
        def exists(self, fileitem):
            calls.append(("exists_dest", fileitem.path))
            return True

        def delete_media_file(self, fileitem):
            calls.append(("delete_dest", fileitem.path))
            return True

    monkeypatch.setattr(
        "app.agent.tools.impl.delete_transfer_history.TransferHistoryOper",
        FakeTransferHistoryOper,
    )
    monkeypatch.setattr(
        "app.agent.tools.impl.delete_transfer_history.StorageChain",
        FakeStorageChain,
    )

    tool = DeleteTransferHistoryTool(session_id="redo-session", user_id="10001")
    result = asyncio.run(tool.run(history_id=9))

    assert "已删除整理历史记录" in result
    assert calls == [
        ("get", 9),
        ("delete_history", 9),
    ]


def test_delete_transfer_history_tool_only_treats_exact_move_as_reorganize_source(monkeypatch):
    """整理方式必须精确等于 move，其他模式仍应清理旧目标文件。"""
    calls = []
    history = SimpleNamespace(
        id=11,
        title="奔跑吧",
        src="/downloads/Keep.Running.mkv",
        status=True,
        mode="not-move",
        dest_fileitem={
            "storage": "local",
            "path": "/library/奔跑吧 (2014)/Keep.Running.mkv",
            "name": "Keep.Running.mkv",
            "type": "file",
        },
    )

    class FakeTransferHistoryOper:
        async def async_get(self, history_id):
            calls.append(("get", history_id))
            return history

        async def async_delete(self, history_id):
            calls.append(("delete_history", history_id))

    class FakeStorageChain:
        def exists(self, fileitem):
            calls.append(("exists_dest", fileitem.path))
            return True

        def delete_media_file(self, fileitem):
            calls.append(("delete_dest", fileitem.path))
            return True

    monkeypatch.setattr(
        "app.agent.tools.impl.delete_transfer_history.TransferHistoryOper",
        FakeTransferHistoryOper,
    )
    monkeypatch.setattr(
        "app.agent.tools.impl.delete_transfer_history.StorageChain",
        FakeStorageChain,
    )

    tool = DeleteTransferHistoryTool(session_id="redo-session", user_id="10001")
    result = asyncio.run(tool.run(history_id=11))

    assert "已删除旧媒体库文件" in result
    assert calls == [
        ("get", 11),
        ("exists_dest", "/library/奔跑吧 (2014)/Keep.Running.mkv"),
        ("delete_dest", "/library/奔跑吧 (2014)/Keep.Running.mkv"),
        ("delete_history", 11),
    ]


def test_manual_redo_context_uses_dest_path_for_successful_move_record():
    """成功 move 记录重新整理时，旧目标文件才是可继续整理的输入路径。"""
    history = SimpleNamespace(
        id=10,
        status=True,
        title="奔跑吧",
        type="电视剧",
        category="综艺",
        year="2014",
        seasons="S01",
        episodes="E01",
        src="/downloads/Keep.Running.mkv",
        src_storage="local",
        src_fileitem={
            "storage": "local",
            "path": "/downloads/Keep.Running.mkv",
            "name": "Keep.Running.mkv",
            "type": "file",
        },
        dest="/library/奔跑吧 (2014)/Keep.Running.mkv",
        dest_storage="local",
        dest_fileitem={
            "storage": "local",
            "path": "/library/奔跑吧 (2014)/Keep.Running.mkv",
            "name": "Keep.Running.mkv",
            "type": "file",
        },
        mode="move",
        tmdbid=100,
        doubanid=None,
        errmsg=None,
    )

    context = build_manual_redo_template_context(history)

    assert context["source_path"] == "/library/奔跑吧 (2014)/Keep.Running.mkv"
    assert context["source_storage"] == "local"


def test_manual_redo_context_only_treats_exact_move_as_dest_source():
    """非 move 整理方式即使名称包含 move，也应继续使用原始来源。"""
    history = SimpleNamespace(
        id=12,
        status=True,
        title="奔跑吧",
        type="电视剧",
        category="综艺",
        year="2014",
        seasons="S01",
        episodes="E01",
        src="/downloads/Keep.Running.mkv",
        src_storage="local",
        src_fileitem={
            "storage": "local",
            "path": "/downloads/Keep.Running.mkv",
            "name": "Keep.Running.mkv",
            "type": "file",
        },
        dest="/library/奔跑吧 (2014)/Keep.Running.mkv",
        dest_storage="local",
        dest_fileitem={
            "storage": "local",
            "path": "/library/奔跑吧 (2014)/Keep.Running.mkv",
            "name": "Keep.Running.mkv",
            "type": "file",
        },
        mode="not-move",
        tmdbid=100,
        doubanid=None,
        errmsg=None,
    )

    context = build_manual_redo_template_context(history)

    assert context["source_path"] == "/downloads/Keep.Running.mkv"
    assert context["source_storage"] == "local"
