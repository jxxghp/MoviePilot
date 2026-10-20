"""
验证同媒体不同种子的移动整理清理边界。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.schemas.transfer import TransferInfo
from tests.test_transfer_job_manager import (
    FakeMedia,
    bind_terminal_checkpoint,
    make_fileitem,
    make_task,
    make_transfer_chain,
)


@pytest.mark.parametrize("same_torrent", [False, True, None])
@pytest.mark.parametrize("sibling_state", ["failed", "running", "waiting"])
def test_successful_move_cleans_only_its_completed_torrent(
    monkeypatch, same_torrent, sibling_state,
):
    """
    不同种子的未完成任务不阻塞成功种子；同种子和无种子作业仍须全部成功。
    """
    chain = make_transfer_chain()
    chain.eventmanager = MagicMock()
    chain.transfer_completed = MagicMock()
    chain.list_torrents = MagicMock(return_value=[])
    chain._can_delete_torrent = MagicMock(return_value=True)
    chain.remove_torrents = MagicMock(return_value=True)
    monkeypatch.setattr(
        "app.chain.transfer.settlement.get_configured_system_config", MagicMock(),
    )
    monkeypatch.setattr(
        "app.chain.transfer.settlement.add_transfer_success",
        lambda **_kwargs: SimpleNamespace(id=1),
    )
    current = make_task(17)
    sibling = make_task(16)
    current.download_hash = "completed-torrent" if same_torrent is not None else None
    sibling.download_hash = current.download_hash if same_torrent else "other-torrent"
    for task in (current, sibling):
        task.mediainfo = FakeMedia()
        task.downloader = "qb"
        assert chain._TransferChain__put_to_jobview(task)
    if sibling_state == "failed":
        chain.jobview.fail_task(sibling)
    elif sibling_state == "running":
        chain.jobview.running_task(sibling)
    result = TransferInfo(
        success=True,
        fileitem=current.fileitem,
        target_item=make_fileitem("/library/Test.Show.S01E17.mkv"),
        transfer_type="move",
        need_notify=False,
        need_scrape=False,
    )
    bind_terminal_checkpoint(current, result)

    state, error = chain._TransferChain__default_callback(current, result)

    assert state is True
    assert error == ""
    assert not chain.jobview.is_success(current)
    if same_torrent is not False:
        chain._can_delete_torrent.assert_not_called()
        chain.remove_torrents.assert_not_called()
    else:
        chain._can_delete_torrent.assert_called_once()
        chain.remove_torrents.assert_called_once_with("completed-torrent", downloader="qb")
