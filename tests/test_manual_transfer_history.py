from types import SimpleNamespace

from app.api.endpoints.transfer import (
    manual_transfer as manual_transfer_endpoint,
    query_manual_transfer_history,
)
from app.chain.transfer import TransferChain
from app.db.transferhistory_oper import TransferHistoryOper
from app.schemas import FileItem, ManualTransferItem
from tests.test_transfer_sync_extra_files import (
    FakeMeta,
    make_fileitem,
    make_transfer_chain,
)


def _patch_transfer_planning(monkeypatch, chain, fileitem, history, planned, deleted):
    """隔离整理规划依赖，并记录历史及旧目标清理动作。"""
    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda current_fileitem, predicate: [(fileitem, False)],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda task: True)
    monkeypatch.setattr(
        chain,
        "_TransferChain__register_scrape_batch_task",
        lambda task: None,
    )
    monkeypatch.setattr(
        chain,
        "_TransferChain__close_scrape_batch",
        lambda batch_id: None,
    )

    def fake_handle_transfer(task, callback=None):
        """记录进入实际整理阶段的文件。"""
        planned.append(task.fileitem.path)
        return True, ""

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", fake_handle_transfer)

    history_oper = SimpleNamespace(
        get_by_src=lambda src, storage=None: history,
        get_by_dest=lambda dest, storage=None: None,
        delete=lambda history_id: deleted.append(("history", history_id)),
    )
    monkeypatch.setattr(
        "app.chain.transfer.TransferHistoryOper",
        lambda: history_oper,
    )
    monkeypatch.setattr(
        "app.chain.transfer.DownloadHistoryOper",
        lambda: SimpleNamespace(
            get_by_hash=lambda download_hash: None,
            get_file_by_fullpath=lambda fullpath: None,
            get_files_by_savepath=lambda savepath: [],
            get_by_path=lambda path: None,
        ),
    )
    monkeypatch.setattr(
        "app.chain.transfer.SystemConfigOper",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr(
        "app.chain.transfer.StorageChain",
        lambda: SimpleNamespace(
            exists=lambda current_fileitem: True,
            delete_media_file=lambda current_fileitem: deleted.append(
                ("target", current_fileitem.path)
            )
            or True,
        ),
    )
    monkeypatch.setattr(
        "app.chain.transfer.MetaInfoPath",
        lambda path, custom_words=None: FakeMeta(1),
    )


def test_query_manual_transfer_history_returns_success_summary(monkeypatch):
    """手动整理初始化接口应只返回成功历史摘要供前端切换重整状态。"""
    fileitem = make_fileitem("/downloads/Test.Show.S01E01.mkv")
    captured = []

    class _FakeTransferChain:
        """记录历史查询收到的文件项。"""

        def get_manual_transfer_histories(self, fileitems):
            """返回两条成功历史记录。"""
            captured.extend(fileitems)
            return [SimpleNamespace(id=1), SimpleNamespace(id=2)]

    monkeypatch.setattr(
        "app.api.endpoints.transfer.TransferChain",
        _FakeTransferChain,
    )

    response = query_manual_transfer_history(
        transer_item=ManualTransferItem(fileitem=fileitem),
        db=object(),
        _="token",
    )

    assert response.success is True
    assert response.data == {"reorganize": True, "history_count": 2}
    assert captured == [fileitem]


def test_manual_transfer_endpoint_passes_reorganize_confirmation(monkeypatch):
    """手动整理接口应把前端重整确认传入整理链。"""
    fileitem = make_fileitem("/downloads/Test.Show.S01E01.mkv")
    captured = {}

    class _FakeTransferChain:
        """记录手动整理调用参数。"""

        def manual_transfer(self, **kwargs):
            """保存整理参数并返回成功。"""
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr(
        "app.api.endpoints.transfer.TransferChain",
        _FakeTransferChain,
    )

    response = manual_transfer_endpoint(
        transer_item=ManualTransferItem(
            fileitem=fileitem,
            reorganize=True,
        ),
        background=False,
        db=object(),
        _="token",
    )

    assert response.success is True
    assert captured["reorganize"] is True


def test_history_endpoint_reorganize_uses_chain_cleanup(monkeypatch):
    """历史 ID 重整应交由整理链清理历史，不能再传入旧目标重复删除。"""
    src_fileitem = make_fileitem("/downloads/Test.Show.S01E01.mkv")
    dest_fileitem = make_fileitem("/library/Test Show/Test.Show.S01E01.mkv")
    history = SimpleNamespace(
        id=21,
        status=True,
        mode="copy",
        src_fileitem=src_fileitem.model_dump(),
        dest_fileitem=dest_fileitem.model_dump(),
    )
    captured = {}

    class _FakeTransferChain:
        """记录历史 ID 重整调用参数。"""

        def manual_transfer(self, **kwargs):
            """保存整理参数并返回成功。"""
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr(
        "app.api.endpoints.transfer.TransferHistory.get",
        lambda db, logid: history,
    )
    monkeypatch.setattr(
        "app.api.endpoints.transfer.TransferChain",
        _FakeTransferChain,
    )

    response = manual_transfer_endpoint(
        transer_item=ManualTransferItem(
            logid=history.id,
            reorganize=True,
        ),
        background=False,
        db=object(),
        _="token",
    )

    assert response.success is True
    assert captured["force"] is True
    assert captured["reorganize"] is True
    assert captured["cleanup_dest_fileitem"] is None


def test_success_history_directory_query_excludes_failed_and_siblings():
    """目录历史查询应限定路径边界，并且只返回成功记录。"""
    transfer_history_oper = TransferHistoryOper()
    created_histories = [
        transfer_history_oper.add_force(
            src="/issue-6191/show/episode-1.mkv",
            src_storage="local",
            status=True,
        ),
        transfer_history_oper.add_force(
            src="/issue-6191/show/episode-2.mkv",
            src_storage="local",
            status=False,
        ),
        transfer_history_oper.add_force(
            src="/issue-6191/show-other/episode-3.mkv",
            src_storage="local",
            status=True,
        ),
    ]
    try:
        histories = transfer_history_oper.list_success_by_src(
            "/issue-6191/show",
            storage="local",
            recursive=True,
        )

        assert [history.src for history in histories] == [
            "/issue-6191/show/episode-1.mkv"
        ]
    finally:
        for history in created_histories:
            transfer_history_oper.delete(history.id)


def test_success_history_directory_query_escapes_sql_wildcards():
    """目录名中的 SQL 通配符应按普通字符匹配，不能扩大查询范围。"""
    transfer_history_oper = TransferHistoryOper()
    created_histories = [
        transfer_history_oper.add_force(
            src="/issue-6191/show_100%/episode-1.mkv",
            src_storage="local",
            status=True,
        ),
        transfer_history_oper.add_force(
            src="/issue-6191/showA100B/episode-2.mkv",
            src_storage="local",
            status=True,
        ),
    ]
    try:
        histories = transfer_history_oper.list_success_by_src(
            "/issue-6191/show_100%",
            storage="local",
            recursive=True,
        )

        assert [history.src for history in histories] == [
            "/issue-6191/show_100%/episode-1.mkv"
        ]
    finally:
        for history in created_histories:
            transfer_history_oper.delete(history.id)


def test_successful_move_history_is_found_by_current_destination():
    """成功移动后从媒体库现址打开整理界面时，应识别原整理历史。"""
    transfer_history_oper = TransferHistoryOper()
    destination = make_fileitem("/library/Test Show/Test.Show.S01E01.mkv")
    history = transfer_history_oper.add_force(
        src="/downloads/Test.Show.S01E01.mkv",
        src_storage="local",
        dest=destination.path,
        dest_storage="local",
        dest_fileitem=destination.model_dump(),
        mode="move",
        status=True,
    )
    try:
        histories = TransferChain.get_manual_transfer_histories(
            make_transfer_chain(),
            [destination],
        )

        assert [current.id for current in histories] == [history.id]
    finally:
        transfer_history_oper.delete(history.id)


def test_manual_transfer_removes_failed_history_before_retry(monkeypatch):
    """失败历史不应阻塞手动整理，并应在重试前清理旧目标和记录。"""
    chain = make_transfer_chain()
    fileitem = make_fileitem("/downloads/Test.Show.S01E01.mkv")
    old_dest = make_fileitem("/library/Test Show/Test.Show.S01E01.mkv")
    history = SimpleNamespace(
        id=7,
        status=False,
        mode="copy",
        dest_fileitem=old_dest.model_dump(),
        download_hash=None,
        downloader=None,
    )
    planned = []
    deleted = []
    _patch_transfer_planning(
        monkeypatch,
        chain,
        fileitem,
        history,
        planned,
        deleted,
    )

    state, message = TransferChain.do_transfer(
        chain,
        fileitem=fileitem,
        background=False,
        manual=True,
    )

    assert state is True
    assert message == ""
    assert deleted == [
        ("target", old_dest.path),
        ("history", history.id),
    ]
    assert planned == [fileitem.path]


def test_automatic_transfer_keeps_failed_history(monkeypatch):
    """自动整理仍应保留失败历史并跳过，避免失败任务自动循环。"""
    chain = make_transfer_chain()
    fileitem = make_fileitem("/downloads/Test.Show.S01E01.mkv")
    history = SimpleNamespace(
        id=12,
        status=False,
        mode="copy",
        dest_fileitem=make_fileitem(
            "/library/Test Show/Test.Show.S01E01.mkv"
        ).model_dump(),
        download_hash=None,
        downloader=None,
    )
    planned = []
    deleted = []
    _patch_transfer_planning(
        monkeypatch,
        chain,
        fileitem,
        history,
        planned,
        deleted,
    )

    state, message = TransferChain.do_transfer(
        chain,
        fileitem=fileitem,
        background=False,
        manual=False,
    )

    assert state is False
    assert message == f"{fileitem.name} 已整理过"
    assert deleted == []
    assert planned == []


def test_manual_transfer_keeps_success_history_without_confirmation(monkeypatch):
    """未携带重整标志时，成功历史仍应阻止普通手动整理。"""
    chain = make_transfer_chain()
    fileitem = make_fileitem("/downloads/Test.Show.S01E01.mkv")
    history = SimpleNamespace(
        id=8,
        status=True,
        mode="copy",
        dest_fileitem=make_fileitem(
            "/library/Test Show/Test.Show.S01E01.mkv"
        ).model_dump(),
        download_hash=None,
        downloader=None,
    )
    planned = []
    deleted = []
    _patch_transfer_planning(
        monkeypatch,
        chain,
        fileitem,
        history,
        planned,
        deleted,
    )

    state, message = TransferChain.do_transfer(
        chain,
        fileitem=fileitem,
        background=False,
        manual=True,
    )

    assert state is True
    assert message == f"{fileitem.name} 已整理过"
    assert deleted == []
    assert planned == []


def test_manual_reorganize_removes_success_history_and_old_target(monkeypatch):
    """用户确认重新整理后，应清理成功历史及非移动模式旧目标再执行。"""
    chain = make_transfer_chain()
    fileitem = make_fileitem("/downloads/Test.Show.S01E01.mkv")
    old_dest = make_fileitem("/library/Wrong Show/Wrong.Show.S02E01.mkv")
    history = SimpleNamespace(
        id=9,
        status=True,
        mode="copy",
        dest_fileitem=old_dest.model_dump(),
        download_hash=None,
        downloader=None,
    )
    planned = []
    deleted = []
    _patch_transfer_planning(
        monkeypatch,
        chain,
        fileitem,
        history,
        planned,
        deleted,
    )

    state, message = TransferChain.do_transfer(
        chain,
        fileitem=fileitem,
        background=False,
        manual=True,
        reorganize=True,
    )

    assert state is True
    assert message == ""
    assert deleted == [
        ("target", old_dest.path),
        ("history", history.id),
    ]
    assert planned == [fileitem.path]


def test_manual_reorganize_keeps_successful_move_target_as_source(monkeypatch):
    """成功移动后的目标是当前重整源，只能删历史记录，不能先删除文件。"""
    chain = make_transfer_chain()
    fileitem = make_fileitem("/library/Test Show/Test.Show.S01E01.mkv")
    history = SimpleNamespace(
        id=10,
        status=True,
        mode="move",
        dest_fileitem=fileitem.model_dump(),
        download_hash=None,
        downloader=None,
    )
    planned = []
    deleted = []
    _patch_transfer_planning(
        monkeypatch,
        chain,
        fileitem,
        history,
        planned,
        deleted,
    )

    state, message = TransferChain.do_transfer(
        chain,
        fileitem=fileitem,
        background=False,
        manual=True,
        reorganize=True,
    )

    assert state is True
    assert message == ""
    assert deleted == [("history", history.id)]
    assert planned == [fileitem.path]


def test_forced_manual_reorganize_still_removes_history(monkeypatch):
    """从历史入口强制重新整理时，仍应按重整确认清理旧目标和记录。"""
    chain = make_transfer_chain()
    fileitem = make_fileitem("/downloads/Test.Show.S01E01.mkv")
    old_dest = make_fileitem("/library/Wrong Show/Wrong.Show.S02E01.mkv")
    history = SimpleNamespace(
        id=11,
        status=True,
        mode="copy",
        dest_fileitem=old_dest.model_dump(),
        download_hash=None,
        downloader=None,
    )
    planned = []
    deleted = []
    _patch_transfer_planning(
        monkeypatch,
        chain,
        fileitem,
        history,
        planned,
        deleted,
    )

    state, message = TransferChain.do_transfer(
        chain,
        fileitem=fileitem,
        background=False,
        manual=True,
        force=True,
        reorganize=True,
    )

    assert state is True
    assert message == ""
    assert deleted == [
        ("target", old_dest.path),
        ("history", history.id),
    ]
    assert planned == [fileitem.path]
