"""
音乐专辑选集的目录与整理链构造；不注册测试用例。
"""

from pathlib import Path
from types import SimpleNamespace
from app.schemas.file import FileItem
from tests.support.transfer.files import bind_empty_history_repositories, make_transfer_chain


def _directory_item(path: Path) -> FileItem:
    """
    构造手动整理使用的专辑目录项。
    """
    return FileItem(
        storage="local",
        path=path.as_posix() + "/",
        type="dir",
        name=path.name,
    )


def _prepare_chain(monkeypatch, fileitems: list[FileItem]):
    """
    隔离文件规划外部依赖，返回可同步观察的整理链。
    """
    chain = make_transfer_chain()
    bind_empty_history_repositories(chain)
    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda _fileitem, predicate: [
            (item, False) for item in fileitems if predicate(item, False)
        ],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda _task: True)
    monkeypatch.setattr(chain, "_register_scrape_batch_task", lambda _task: None)
    monkeypatch.setattr(chain, "_close_scrape_batch", lambda _batch_id: None)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: None),
    )
    return chain
