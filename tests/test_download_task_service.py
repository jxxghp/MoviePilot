"""下载任务应用服务测试。"""

from types import SimpleNamespace

from app.application.download.tasks import DownloadTaskService


def test_download_task_service_enriches_history_and_controls_task():
    """下载任务查询应附加历史媒体信息，控制方法只转发规范参数。"""
    torrent = SimpleNamespace(hash="hash", media=None)
    history = SimpleNamespace(
        media_source="tmdb",
        media_id="123",
        type="电影",
        title="测试电影",
        seasons=[1],
        episodes=[2],
        poster="poster",
        image="backdrop",
        torrent_site="站点",
        userid=1,
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
    assert torrent.media["media_id"] == "123"
    assert torrent.username == "alice"
    assert service.set_downloading("hash", "start", "qb") is True
    assert service.set_downloading("hash", "stop", "qb") is True
    assert service.remove_downloading("hash", "qb") is True
    assert [call[0] for call in calls] == ["start", "stop", "remove"]


def test_download_task_service_rejects_unknown_operation():
    """未知操作保持旧的 False 返回语义。"""
    service = DownloadTaskService(
        list_torrents=lambda **_kwargs: [],
        get_history_by_hashes=lambda _hashes: {},
        start_torrents=lambda **_kwargs: True,
        stop_torrents=lambda **_kwargs: True,
        remove_torrents=lambda **_kwargs: True,
    )

    assert service.set_downloading("hash", "pause") is False
