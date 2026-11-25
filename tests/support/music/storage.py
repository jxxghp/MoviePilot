"""
音乐目录整理的隔离链装配；不注册测试用例。
"""

from types import SimpleNamespace

from app.chain.media import MediaChain
from tests.support.transfer.files import bind_empty_history_repositories, make_transfer_chain


def _setup_transfer(monkeypatch, items):
    """
    隔离历史和存储枚举，保留实际候选规划及识别路径。
    """
    owner = make_transfer_chain()
    bind_empty_history_repositories(owner)
    owner.eventmanager = SimpleNamespace(send_event=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(owner, "_TransferChain__get_trans_fileitems", lambda *_args, **_kwargs: [(item, False) for item in items])
    monkeypatch.setattr(owner, "_TransferChain__put_to_jobview", lambda _task: True)
    monkeypatch.setattr(owner, "_register_scrape_batch_task", lambda _task: None)
    monkeypatch.setattr(owner, "_close_scrape_batch", lambda _batch: None)
    monkeypatch.setattr("app.chain.transfer.workflow.get_configured_system_config", lambda: SimpleNamespace(get=lambda _key: None))
    monkeypatch.setattr("app.chain.transfer.request._TransferCandidatePlanner._get_single_file_sibling_items", lambda *_args: (items, []))
    monkeypatch.setattr(MediaChain, "supplement_tmdb_info", lambda _self, media, _meta: media)
    return owner
