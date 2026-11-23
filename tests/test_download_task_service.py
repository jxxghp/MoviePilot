"""
下载任务应用服务与 ChainBase 任务操作锁测试。
"""

from unittest.mock import Mock

import pytest

from app.application.download.tasks import DownloadTaskService
from app.application.history import DownloadHistorySnapshot
from app.chain.base import ChainBase
from app.runtime.locks import downloader_task_locks
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import MediaSource


def test_download_task_service_enriches_history_and_controls_task():
    """
    下载任务查询应附加历史媒体信息，控制方法只转发规范参数。
    """
    torrent = DownloaderTorrent(hash="hash")
    history = DownloadHistorySnapshot(
        id=1,
        path="/downloads/test",
        media_source=MediaSource.TMDB,
        media_id="123",
        type="电影",
        title="测试电影",
        seasons="1",
        episodes="2",
        poster="poster",
        image="backdrop",
        torrent_site="站点",
        userid="1",
        username="alice",
    )
    calls = []
    service = DownloadTaskService(
        list_torrents=lambda **kwargs: [torrent],
        get_history_by_hashes=lambda hashes: {hashes[0]: history},
        start_torrents=lambda **kwargs: calls.append(("start", kwargs)) or True,
        stop_torrents=lambda **kwargs: calls.append(("stop", kwargs)) or True,
        remove_torrents=lambda **kwargs: calls.append(("remove", kwargs)) or True,
    )

    assert service.downloading("qb") == [torrent]
    assert torrent.media is not None
    assert torrent.media.media_id == "123"
    assert torrent.username == "alice"
    assert service.set_downloading("hash", "start", "qb") is True
    assert service.set_downloading("hash", "stop", "qb") is True
    assert service.remove_downloading("hash", "qb") is True
    assert [call[0] for call in calls] == ["start", "stop", "remove"]


def test_download_task_service_rejects_unknown_operation():
    """
    未知操作保持旧的 False 返回语义。
    """
    service = DownloadTaskService(
        list_torrents=lambda **_kwargs: [],
        get_history_by_hashes=lambda _hashes: {},
        start_torrents=lambda **_kwargs: True,
        stop_torrents=lambda **_kwargs: True,
        remove_torrents=lambda **_kwargs: True,
    )

    assert service.set_downloading("hash", "pause") is False


@pytest.mark.parametrize("method,args", [
    ("stop_torrents", (["HASH"], "qb")),
    ("start_torrents", (["HASH"], "qb")),
    ("set_torrents_tag", ("HASH", ["标签"], "qb")),
    ("update_torrent", ("HASH", "qb")),
    ("remove_torrents", ("HASH", False, "qb")),
    ("transfer_completed", ("HASH", "qb")),
])
def test_chain_task_mutations_hold_task_lock(method, args):
    """
    ChainBase 上改动下载任务的入口都按 hash 持有任务锁，与订阅追加文件互斥。

    否则用户刚点了暂停，订阅会凭暂停前读到的"运行中"往任务里追加文件并记成已下载；
    Transmission 只能整体覆盖标签，交错写标签时后写的会覆盖先写的。
    """
    stripe = downloader_task_locks._locks[downloader_task_locks._stripe("HASH")]
    held = []
    chain = ChainBase.__new__(ChainBase)
    chain.run_module = Mock(side_effect=lambda *_args, **_kwargs: held.append(stripe._is_owned()) or True)

    getattr(chain, method)(*args)

    assert held == [True]
    assert not stripe._is_owned()
