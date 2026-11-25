"""
Transmission 任务状态过滤与 transmission-rpc 7 原始状态值的对齐回归。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from transmission_rpc.torrent import Status

from app.modules.transmission import TransmissionModule
from app.modules.transmission.transmission import Transmission
from app.schemas.types import DownloadTaskState


def _client(*torrents: SimpleNamespace) -> Transmission:
    """
    构造只返回给定任务的离线 Transmission 客户端。
    """
    client = Transmission.__new__(Transmission)
    client.trc = MagicMock()
    client.trc.get_torrents.return_value = list(torrents)
    return client


def test_completed_torrents_include_queued_seeding():
    """
    排队做种（seed pending）的任务已下载完成，必须进入待整理列表。
    """
    seeding = SimpleNamespace(status=Status.SEEDING, labels=[])
    queued = SimpleNamespace(status=Status.SEED_PENDING, labels=[])
    downloading = SimpleNamespace(status=Status.DOWNLOADING, labels=[])

    assert _client(seeding, queued, downloading).get_completed_torrents() == [seeding, queued]


def test_downloading_torrents_include_queued_download():
    """
    排队下载（download pending）的任务仍属于下载中列表。
    """
    downloading = SimpleNamespace(status=Status.DOWNLOADING, labels=[])
    queued = SimpleNamespace(status=Status.DOWNLOAD_PENDING, labels=[])
    seeding = SimpleNamespace(status=Status.SEEDING, labels=[])

    assert _client(downloading, queued, seeding).get_downloading_torrents() == [downloading, queued]


@pytest.mark.parametrize("status, expected", [
    (Status.DOWNLOAD_PENDING, DownloadTaskState.DOWNLOADING),
    (Status.DOWNLOADING, DownloadTaskState.DOWNLOADING),
    (Status.STOPPED, DownloadTaskState.PAUSED),
    (Status.SEED_PENDING, DownloadTaskState.COMPLETED),
])
def test_normalize_state_uses_transmission_rpc_status_values(status: Status, expected: DownloadTaskState):
    """
    排队下载的任务不得被归一为已完成。
    """
    normalize = getattr(TransmissionModule, "_TransmissionModule__normalize_torrent_state")

    assert normalize(status) == expected.value
