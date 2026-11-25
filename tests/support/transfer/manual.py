"""
手工整理规划端口装配；不注册测试用例。
"""

from types import SimpleNamespace
from tests.support.transfer.files import FakeMeta


def _patch_transfer_planning(monkeypatch, chain, fileitem, history, planned, deleted):
    """
    隔离整理规划依赖，并记录历史及旧目标清理动作。
    """
    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda current_fileitem, predicate: [(fileitem, False)],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda task: True)
    monkeypatch.setattr(
        chain,
        "_register_scrape_batch_task",
        lambda task: None,
    )
    monkeypatch.setattr(
        chain,
        "_close_scrape_batch",
        lambda batch_id: None,
    )

    def fake_handle_transfer(task, callback=None):
        """
        记录进入实际整理阶段的文件。
        """
        planned.append(task.fileitem.path)
        return True, ""

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", fake_handle_transfer)

    history_oper = SimpleNamespace(
        get_by_src=lambda src, storage=None: history,
        get_success_by_src=lambda src, storage=None: history if history and history.status else None,
        get_by_dest=lambda dest, storage=None: None,
        delete=lambda history_id: deleted.append(("history", history_id)),
    )
    history_oper.list_success_by_src = lambda src, storage=None, recursive=False: (
        [record] if (record := history_oper.get_success_by_src(src, storage=storage)) else []
    )
    history_oper.list_success_move_by_dest = lambda dest, storage=None, recursive=False: (
        [record] if chain._is_successful_move_history(record := history_oper.get_by_dest(dest, storage=storage)) else []
    )
    chain.transfer_history_repository = history_oper
    chain.download_history_repository = SimpleNamespace(
        get_by_hash=lambda download_hash: None,
        get_file_by_fullpath=lambda fullpath: None,
        get_files_by_savepath=lambda savepath: [],
        get_by_path=lambda path: None,
    )
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda key: None),
    )
    monkeypatch.setattr("app.chain.transfer.format.get_configured_system_config", lambda: SimpleNamespace(get=lambda key: None))
    monkeypatch.setattr(
        "app.chain.transfer.settlement.StorageChain",
        lambda: SimpleNamespace(
            exists=lambda current_fileitem: True,
            delete_media_file=lambda current_fileitem: deleted.append(
                ("target", current_fileitem.path)
            )
            or True,
        ),
    )
    monkeypatch.setattr("app.chain.transfer.records.StorageChain", lambda: SimpleNamespace(
        get_file_item_strict=lambda **_kwargs: fileitem,
        delete_media_file=lambda current_fileitem: deleted.append(
            ("target", current_fileitem.path)
        )
        or True,
    ))
    monkeypatch.setattr(
        "app.chain.transfer.request.MetaInfoPath",
        lambda path, custom_words=None, **kwargs: FakeMeta(1),
    )
