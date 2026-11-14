"""
三个下载器在选集、附属文件保留和已有任务保护上的一致性回归。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from torrentool.bencode import Bencode

from app.chain.download.facade import DownloadChain
from app.domain import torrent as torrent_rules
from app.domain.context import Context
from app.modules.qbittorrent import QbittorrentModule
from app.modules.rtorrent import RtorrentModule
from app.modules.transmission import TransmissionModule
from app.schemas.download import DOWNLOAD_TASK_ALREADY_EXISTS


@pytest.fixture(params=[QbittorrentModule, TransmissionModule, RtorrentModule])
def selected_downloader(request):
    """
    构造真实模块和离线下载器边界，保留下载链的识别词传递。
    """
    module = request.param.__new__(request.param)
    content = Bencode.encode({"info": {
        "name": "Show.S01", "piece length": 16384, "pieces": b"x" * 20,
        "files": [{"length": 42, "path": ["Show.S01E05.mkv"]}],
    }})
    torrent_hash = next(iter(torrent_rules.download_hashes(content)))
    server = Mock()
    server.add_torrent.return_value = (True, [torrent_hash]) if request.param is QbittorrentModule else True
    server.add_torrent_with_status.return_value = (SimpleNamespace(hashString=torrent_hash), False)
    server.get_torrent_id_by_tag.return_value = torrent_hash
    server.get_content_layout.return_value = "Original"
    server.is_force_resume.return_value = False
    server.ensure_torrent_state.side_effect = (
        lambda torrent_hash, *, paused:
        server.stop_torrents(torrent_hash) if paused else server.start_torrents(torrent_hash)
    )
    module.get_instance = Mock(return_value=server)
    module.normalize_path = Mock(return_value="/downloads")
    module.get_default_config_name = Mock(return_value="test-downloader")
    return module, server, content, torrent_hash


def _set_files(module, server, names: list[str]) -> None:
    """
    按各客户端的文件对象协议构造列表，目录集号不参与匹配。
    """
    files = [{"id": index, "name": f"Folder.S09E99/{name}"} for index, name in enumerate(names)]
    server.get_files.side_effect = lambda *_args, **_kwargs: (
        [SimpleNamespace(**item) for item in files]
        if isinstance(module, TransmissionModule) else list(files)
    )


def _assert_selected_files(module, server, torrent_hash, wanted, unwanted) -> None:
    """
    验证真实模块发送的文件选择 RPC，防止附属文件落入不下载列表。
    """
    if isinstance(module, TransmissionModule):
        server.set_files.assert_called_once_with(torrent_hash, wanted)
        server.set_unwanted_files.assert_called_once_with(torrent_hash, unwanted)
    else:
        priorities = {call.kwargs["priority"]: call.kwargs["file_ids"] for call in server.set_files.call_args_list}
        assert priorities == {1: wanted, **({0: unwanted} if unwanted else {})}
    server.start_torrents.assert_called_once_with(torrent_hash)


def test_cross_boundary_video_keeps_unnumbered_attachments(selected_downloader) -> None:
    """
    目标 E05 应选中 E05E06，并保留跨目录的字体、字幕和 NFO，排除其它视频。
    """
    module, server, content, torrent_hash = selected_downloader
    _set_files(module, server, [
        "Show.S01E05E06.MKV", "Show.S01E07.mkv", "Fonts/Fonts.7z",
        "Subtitles/Show.S01.ass", "Show.nfo", "Extras/Behind the Scenes.mp4",
        "Show.S01E07.srt", "Show.S01E05.srt",
    ])

    result = module.download(content, Path("/downloads"), "", episodes={5})

    assert result[1] == torrent_hash
    _assert_selected_files(module, server, torrent_hash, [0, 2, 3, 4, 7], [1, 5, 6])


@pytest.mark.parametrize("names,episodes", [
    (["Show.S01E05E06.mkv", "Show.nfo"], {5, 9}),
    (["Show.S01E05E06.mkv", "Show.nfo"], {9}),
    (["Show.S01E05.ass", "Fonts.7z"], {5}),
    (["Show.S01E05.mkv", "Fonts.7z"], set()),
])
def test_incomplete_video_coverage_never_starts(selected_downloader, names, episodes) -> None:
    """
    附属文件不能代替视频证明覆盖，缺集和空目标均不得执行文件设置或启动。
    """
    module, server, _content, torrent_hash = selected_downloader
    _set_files(module, server, names)
    if isinstance(module, RtorrentModule):
        selected, _message = module._select_episode_files(server, torrent_hash, episodes)
    else:
        selected, _message = module._select_download_files(server, torrent_hash, episodes, None)

    assert selected is False
    server.set_files.assert_not_called()
    server.set_unwanted_files.assert_not_called()
    server.start_torrents.assert_not_called()


@pytest.mark.parametrize("video", ["Show.S01E54.mkv", "Show.mkv"])
def test_unique_video_fallback_keeps_unnumbered_attachments(selected_downloader, video) -> None:
    """
    唯一视频兜底在三个下载器上一致，并保留字体及无集号字幕。
    """
    module, server, content, torrent_hash = selected_downloader
    _set_files(module, server, [video, "Fonts.7z", "Show.ass"])

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[1] == torrent_hash
    _assert_selected_files(module, server, torrent_hash, [1, 2, 0], [])


def test_subscription_words_reach_all_downloader_modules(selected_downloader) -> None:
    """
    从下载链传入识别词，让多视频种子的修正集号在三个下载器上均能选中。
    """
    module, server, content, torrent_hash = selected_downloader
    _set_files(module, server, ["Show.S01E54.mkv", "Show.S01E55.mkv", "Fonts.7z"])
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: getattr(module, method)(**kwargs)
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()
    prepared = SimpleNamespace(
        torrent_content=content, torrent=SimpleNamespace(site_cookie=""),
        download_dir=Path("/downloads"), media=SimpleNamespace(category="TV"), site_downloader=None,
    )

    result, _message = chain._submit_prepared_download(
        prepared=prepared, context=Context(), episodes={6}, channel=None, source="subscribe",
        downloader="test-downloader", userid=None, username=None, label=None,
        custom_words="S01E54 => S01E06", governance=None,
    )

    assert result == torrent_hash
    _assert_selected_files(module, server, torrent_hash, [0, 2], [1])
    chain._settle_accepted_download.assert_called_once()
    chain._record_rejected_download.assert_not_called()


@pytest.mark.parametrize("status", ["downloading", "seeding", "stopped"])
def test_existing_task_preserves_state_and_files(selected_downloader, status) -> None:
    """
    相同种子的下载、做种和暂停任务均只返回存在标志，不取文件、重选或启停。
    """
    module, server, content, torrent_hash = selected_downloader
    server.add_torrent.return_value = (False, []) if isinstance(module, QbittorrentModule) else False
    server.add_torrent_with_status.return_value = (None, False)
    server.get_torrents.return_value = (
        [SimpleNamespace(hashString=torrent_hash, name="Show", labels=[], status=status)]
        if isinstance(module, TransmissionModule) else
        [{"hash": torrent_hash, "name": "Show", "tags": "", "state": status}], False,
    )

    result = module.download(content, Path("/downloads"), "", episodes={5}, custom_words="E05 => E99")

    assert result[1] == torrent_hash
    assert result[3] == DOWNLOAD_TASK_ALREADY_EXISTS
    for method in ("get_files", "set_files", "set_unwanted_files", "start_torrents", "stop_torrents",
                   "ensure_torrent_state", "torrents_set_force_start"):
        getattr(server, method).assert_not_called()


def test_episode_selection_preserves_input_sets() -> None:
    """
    共享纯规则不改写文件识别结果或调用方的目标集合。
    """
    file_episodes = {5, 6}
    episodes = {5}

    assert torrent_rules.select_episode_files([(0, file_episodes, True)], episodes) == ([0], [], {5, 6})
    assert file_episodes == {5, 6}
    assert episodes == {5}
