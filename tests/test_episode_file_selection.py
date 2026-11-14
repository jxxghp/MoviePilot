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
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.modules.qbittorrent import QbittorrentModule
from app.modules.rtorrent import RtorrentModule
from app.modules.transmission import TransmissionModule
from app.schemas.download import DOWNLOAD_TASK_ALREADY_EXISTS
from app.schemas.types import MediaType


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
                   "ensure_torrent_state", "torrents_set_force_start", "delete_torrents"):
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


@pytest.mark.parametrize("source,target,requested,wanted", [
    ("Show.S01E20", "Show.S02E08", {8}, [19]),
    ("Show.S01E13-E24", "Show.S02E01-E12", {5}, [16]),
    ("Show.S01E20", "Show.S01E08", {8}, [19]),
])
def test_recognized_season_episode_mapping_reaches_selection(
        selected_downloader, source, target, requested, wanted,
) -> None:
    """
    真实下载链须按已识别的对应关系选择累计集号，不能误选同号的早期剧集。
    """
    module, server, content, torrent_hash = selected_downloader
    _set_files(module, server, [f"Show.S01E{episode:02}.mkv" for episode in range(1, 25)])
    context = Context(meta_info=MetaInfo(target), torrent_info=TorrentInfo(title=source),
                      media_info=MediaInfo(type=MediaType.TV))
    prepared = SimpleNamespace(
        torrent_content=content, torrent=context.torrent_info,
        download_dir=Path("/downloads"), media=context.media_info, site_downloader=None,
    )
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: getattr(module, method)(**kwargs)
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()

    result, _message = chain._submit_prepared_download(
        prepared=prepared, context=context, episodes=requested, channel=None, source="subscribe",
        downloader="test-downloader", userid=None, username=None, label=None,
        custom_words=None, governance=None,
    )

    assert result == torrent_hash
    _assert_selected_files(module, server, torrent_hash, wanted, [i for i in range(24) if i not in wanted])
    chain._settle_accepted_download.assert_called_once()


@pytest.mark.parametrize("names", [
    ["Show.S01E08.mkv"],
    ["Show.S01E08.mkv", "Show.S01E20.mkv"],
])
def test_unmapped_season_mismatch_rejects_and_releases_new_task(selected_downloader, names) -> None:
    """
    无对应关系时拒绝跨季同号文件，清理本次新增任务并将失败交回通知与退避流程。
    """
    module, server, content, torrent_hash = selected_downloader
    _set_files(module, server, names)
    selection = torrent_rules.episode_selection_for_context(Context(
        meta_info=MetaInfo("Show.S02"), torrent_info=TorrentInfo(title="Show.S01"),
    ))

    result = module.download(content, Path("/downloads"), "", episodes={8}, episode_selection=selection)

    assert result[1] is None
    assert "季集" in result[3]
    assert "已请求移除" in result[3]
    server.delete_torrents.assert_called_once_with(delete_file=False, ids=torrent_hash)
    server.set_files.assert_not_called()
    server.start_torrents.assert_not_called()


def test_season_mapping_does_not_double_apply_custom_words(selected_downloader) -> None:
    """
    文件本身已被来源识别词修正时，不能再次应用种子标题的集数映射。
    """
    module, server, content, torrent_hash = selected_downloader
    _set_files(module, server, ["Show.S01E20.mkv", "Show.S01E08.mkv"])
    selection = torrent_rules.episode_selection_for_context(Context(
        meta_info=MetaInfo("Show.S02E08"), torrent_info=TorrentInfo(title="Show.S01E20"),
    ))

    result = module.download(content, Path("/downloads"), "", episodes={8}, episode_selection=selection,
                             custom_words="S01E20 => S02E08")

    assert result[1] == torrent_hash
    _assert_selected_files(module, server, torrent_hash, [0], [1])


@pytest.mark.parametrize("names,selection,wanted", [
    (["Show.S01E08.mkv", "Show.S02E08.mkv", "Show.S02.ass"], torrent_rules.EpisodeSelection(2), [1, 2]),
    (["Show.S01E08.mkv", "Show.S00E08.mkv"], torrent_rules.EpisodeSelection(0), [1]),
    (["Show.E20.mkv", "Show.E08.mkv"], torrent_rules.EpisodeSelection(2, None, ((20, 8),)), [0]),
    (["Show.S01E08.mkv", "Show.S01E20.mkv", "Show.S01E20.srt", "Show.S01E08.srt", "Fonts.7z"],
     torrent_rules.EpisodeSelection(2, 1, ((20, 8),)), [1, 2, 4]),
])
def test_season_selection_preserves_attachments_and_specials(selected_downloader, names, selection, wanted) -> None:
    """
    季号、特别篇、无季累计编号和带集号字幕使用同一映射，未编号附件继续保留。
    """
    module, server, content, torrent_hash = selected_downloader
    _set_files(module, server, names)

    result = module.download(content, Path("/downloads"), "", episodes={8}, episode_selection=selection)

    assert result[1] == torrent_hash
    _assert_selected_files(module, server, torrent_hash, wanted, [i for i in range(len(names)) if i not in wanted])


def test_selection_failure_cleanup_failure_is_reported(selected_downloader) -> None:
    """
    下载器拒绝清理时保留真实状态说明，不能声称任务已移除或提交成功。
    """
    module, server, content, torrent_hash = selected_downloader
    _set_files(module, server, ["Show.S01E08.mkv"])
    server.delete_torrents.return_value = False

    result = module.download(content, Path("/downloads"), "", episodes={8},
                             episode_selection=torrent_rules.EpisodeSelection(2))

    assert result[1] is None
    assert "移除本次新增任务失败" in result[3]
    assert "已请求移除" not in result[3]
    server.delete_torrents.assert_called_once_with(delete_file=False, ids=torrent_hash)


def test_missing_mapping_notifies_and_records_download_backoff(selected_downloader) -> None:
    """
    跨季拒绝清理新任务后，真实下载链保留具体原因并触发失败通知和退避记账。
    """
    module, server, content, torrent_hash = selected_downloader
    _set_files(module, server, ["Show.S01E08.mkv", "Show.S01E20.mkv"])
    context = Context(meta_info=MetaInfo("Show.S02E08"), torrent_info=TorrentInfo(title="Show.S01"),
                      media_info=MediaInfo(type=MediaType.TV, title="Show"))
    prepared = SimpleNamespace(torrent_content=content, torrent=context.torrent_info,
                               meta=context.meta_info, media=context.media_info,
                               download_dir=Path("/downloads"), site_downloader=None)
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: getattr(module, method)(**kwargs)
    chain._settle_accepted_download = Mock()
    chain._record_download_failure = Mock()
    chain.post_message = Mock()

    result, message = chain._submit_prepared_download(
        prepared=prepared, context=context, episodes={8}, channel=None, source="subscribe",
        downloader="test-downloader", userid=None, username=None, label=None,
        custom_words=None, governance=None,
    )

    assert result is None
    assert "季集映射" in message
    assert chain._record_download_failure.call_args.kwargs["error_msg"] == message
    assert message in chain.post_message.call_args.args[0].text
    chain._settle_accepted_download.assert_not_called()
    server.delete_torrents.assert_called_once_with(delete_file=False, ids=torrent_hash)


@pytest.mark.parametrize("title", [None, "Show.S01-S02"])
def test_unknown_or_multiseason_context_does_not_invent_mapping(title) -> None:
    """
    缺少元数据或包含多个目标季时，不能从集号数字推算单季映射。
    """
    context = Context(meta_info=MetaInfo(title) if title else None)
    assert torrent_rules.episode_selection_for_context(context) is None
