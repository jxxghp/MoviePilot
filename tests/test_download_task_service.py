"""
下载任务应用服务与暂停归属接口测试。
"""

from unittest.mock import Mock

import pytest

from app.application.download.tasks import DownloadTaskMutationService, DownloadTaskService
from app.application.history import DownloadHistorySnapshot
from app.chain.base import ChainBase
from app.schemas.download import DownloadPauseIntent, DownloadTaskActor
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


def test_stop_records_actor_only_when_given():
    """
    暂停时把操作方交给下载器写入 MP 暂停标签；未提供操作方时不写标签（会被当成外部暂停）。
    """
    calls = []
    service = DownloadTaskService(
        list_torrents=lambda **_kwargs: [],
        get_history_by_hashes=lambda _hashes: {},
        start_torrents=lambda **kwargs: calls.append(("start", kwargs)) or True,
        stop_torrents=lambda **kwargs: calls.append(("stop", kwargs)) or True,
        remove_torrents=lambda **_kwargs: True,
    )

    service.set_downloading("hash", "stop", "qb", actor=DownloadTaskActor.USER)
    service.set_downloading("hash", "stop", "qb")
    service.set_downloading("hash", "start", "qb", actor=DownloadTaskActor.USER)

    assert calls == [
        ("stop", {"hashs": ["hash"], "downloader": "qb", "actor": DownloadTaskActor.USER}),
        ("stop", {"hashs": ["hash"], "downloader": "qb"}),
        ("start", {"hashs": ["hash"], "downloader": "qb"}),
    ]


def test_advanced_update_pauses_as_user():
    """
    高级更新只由管理用户在 MP 中发起，暂停按用户放弃该资源处理。
    """
    torrent = DownloaderTorrent(hash="a" * 40, downloader="qb")
    set_downloading = Mock(return_value=True)
    mutation = DownloadTaskMutationService(
        list_torrents=lambda **_kwargs: [torrent],
        set_tags=Mock(return_value=True),
        set_downloading=set_downloading,
        update_torrent=Mock(return_value={}),
    )

    mutation.update(hash_value=torrent.hash, action="stop")

    assert set_downloading.call_args.kwargs["actor"] is DownloadTaskActor.USER


@pytest.mark.parametrize("actor,intent,plugin_id,tag", [
    (None, DownloadPauseIntent.ABANDON, None, None),
    (DownloadTaskActor.USER, DownloadPauseIntent.ABANDON, None, "MP暂停:放弃:用户"),
    (DownloadTaskActor.SYSTEM, DownloadPauseIntent.RESUMABLE, None, "MP暂停:可恢复:系统"),
    (DownloadTaskActor.PLUGIN, DownloadPauseIntent.ABANDON, "AutoClean", "MP暂停:放弃:插件:AutoClean"),
    (DownloadTaskActor.PLUGIN, DownloadPauseIntent.DEFER, "NightOnly", "MP暂停:暂缓:插件:NightOnly"),
])
def test_chain_stop_torrents_passes_pause_tag(actor, intent, plugin_id, tag):
    """
    插件与宿主通过 ChainBase.stop_torrents 声明暂停意图与暂停方，转换为下载器上的暂停标签。
    """
    chain = ChainBase.__new__(ChainBase)
    chain.run_module = Mock(return_value=True)

    assert chain.stop_torrents("hash", "qb", actor=actor, intent=intent, plugin_id=plugin_id) is True

    expected = {"hashs": "hash", "downloader": "qb", **({"pause_tag": tag} if tag else {})}
    chain.run_module.assert_called_once_with("stop_torrents", **expected)


@pytest.mark.parametrize("actor,plugin_id", [(DownloadTaskActor.EXTERNAL, None), (DownloadTaskActor.PLUGIN, None)])
def test_chain_stop_torrents_rejects_invalid_actor(actor, plugin_id):
    """
    外部暂停不能由 MP 冒充；插件暂停必须给出插件 ID，否则拒绝且不暂停任务。
    """
    chain = ChainBase.__new__(ChainBase)
    chain.run_module = Mock(return_value=True)

    with pytest.raises(ValueError):
        chain.stop_torrents("hash", "qb", actor=actor, plugin_id=plugin_id)
    chain.run_module.assert_not_called()


@pytest.mark.parametrize("method,kwargs", [
    ("stop_torrents", {"actor": DownloadTaskActor.USER}),
    ("stop_torrents", {}),
    ("start_torrents", {}),
])
def test_chain_pause_and_resume_hold_task_lock(method, kwargs):
    """
    暂停、恢复与订阅追加文件按 hash 互斥：否则用户刚暂停，订阅又凭暂停前读到的标签把任务恢复。
    """
    from app.runtime.locks import downloader_task_locks

    stripe = downloader_task_locks._locks[downloader_task_locks._stripe("hash")]
    held = []
    chain = ChainBase.__new__(ChainBase)
    chain.run_module = Mock(side_effect=lambda *_args, **_kwargs: held.append(stripe.locked()) or True)

    assert getattr(chain, method)(["HASH"], "qb", **kwargs) is True

    assert held == [True]
    assert not stripe.locked()


def test_plugin_sdk_exports_pause_attribution_types():
    """
    插件从稳定 SDK 入口即可拿到暂停方与暂停意图类型。
    """
    from app.sdk import chain as sdk_chain

    assert sdk_chain.DownloadTaskActor is DownloadTaskActor
    assert sdk_chain.DownloadPauseIntent is DownloadPauseIntent
