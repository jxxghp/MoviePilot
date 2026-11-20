"""
三个下载器在选集、附属文件保留和已有任务增量追加上的一致性回归。
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
from app.modules import transmission as transmission_module
from app.modules._base import downloader as downloader_base
from app.modules.qbittorrent import QbittorrentModule
from app.modules.rtorrent import RtorrentModule
from app.modules.transmission import TransmissionModule
from app.schemas.download import (
    DOWNLOAD_TASK_ALREADY_EXISTS,
    DOWNLOAD_TASK_FILES_APPENDED,
    DOWNLOAD_TASK_RESUMED,
    ExistingTaskRejection,
)
from app.schemas.transfer import DownloaderTorrent
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
    # 下载器里还没有同一任务；rTorrent 添加前会按 hash 查一次（load.raw 异步判重，见模块说明）。
    server.get_torrents.return_value = ([], False)
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


def _offline_chain(module):
    """
    构造只连到真实下载器模块的下载链：没有下载历史，提交前读取确认记录时返回空结果。
    """
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: getattr(module, method)(**kwargs)
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_hashes.return_value = {}
    return chain


def test_subscription_words_reach_all_downloader_modules(selected_downloader) -> None:
    """
    从下载链传入识别词，让多视频种子的修正集号在三个下载器上均能选中。
    """
    module, server, content, torrent_hash = selected_downloader
    _set_files(module, server, ["Show.S01E54.mkv", "Show.S01E55.mkv", "Fonts.7z"])
    chain = _offline_chain(module)
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


def existing_task(module, server, torrent_hash, names, *, wanted=(), completed=(), stopped=False, tags=(), error=False):
    """
    让真实模块把本次添加解释为已有任务，并用可变状态模拟下载器的文件选择、启停与标签。

    文件写入、启动和标签读写都会改变模拟状态，使模块写入后的回读确认能看到真实效果。

    :param module: 当前参数化的下载器模块
    :param server: 被替换的外部下载器客户端
    :param torrent_hash: 已有任务的哈希
    :param names: 种子内文件的相对路径
    :param wanted: 当前已选中的文件索引
    :param completed: 当前已下载完成的文件索引
    :param stopped: 任务当前是否处于暂停状态
    :param tags: 任务当前的标签
    :param error: 下载器是否报告任务出错（qB missingFiles、TR 本地错误；rT 没有错误状态）
    :return: 文件状态（索引到 wanted/completed）与任务状态（stopped/tags）两个可变字典
    """
    files = {index: {"wanted": index in wanted, "completed": index in completed} for index in range(len(names))}
    task = {"stopped": stopped, "tags": list(tags)}

    def read_files(*_args, **_kwargs):
        """
        按各客户端的文件对象协议投影当前模拟状态。
        """
        if isinstance(module, TransmissionModule):
            return [
                SimpleNamespace(id=index, name=name, selected=files[index]["wanted"],
                                completed=42 if files[index]["completed"] else 0, size=42)
                for index, name in enumerate(names)
            ]
        scale = 1 if isinstance(module, QbittorrentModule) else 100
        return [
            {"id": index, "name": name, "priority": 1 if files[index]["wanted"] else 0,
             "progress": scale if files[index]["completed"] else 0}
            for index, name in enumerate(names)
        ]

    def write_files(*args, **kwargs):
        """
        记录被设为下载或不下载的文件。
        """
        file_ids = kwargs["file_ids"] if "file_ids" in kwargs else args[1]
        for file_id in file_ids:
            files[file_id]["wanted"] = kwargs.get("priority", 1) > 0
        return True

    def start(*_args, **_kwargs):
        """
        启动任务后状态变为运行。
        """
        task["stopped"] = False
        return True

    def stop(*_args, **_kwargs):
        """
        暂停任务。
        """
        task["stopped"] = True
        return True

    def add_tags(*_args, ids=None, tags=(), **_kwargs):
        """
        qB/rT 追加标签（rT 客户端在内部合并 d.custom1）。
        """
        task["tags"] += [tag for tag in tags if tag not in task["tags"]]
        return True

    def remove_tags(*_args, ids=None, tag=(), **_kwargs):
        """
        qB/rT 移除指定标签。
        """
        removed = [tag] if isinstance(tag, str) else list(tag)
        task["tags"] = [item for item in task["tags"] if item not in removed]
        return True

    def replace_labels(*_args, ids=None, labels=(), **_kwargs):
        """
        Transmission 整体覆盖标签。
        """
        task["tags"] = list(labels)
        return True

    def read_torrents(*_args, **_kwargs):
        """
        返回带当前启停状态与标签的已有任务。
        """
        if isinstance(module, TransmissionModule):
            torrent = SimpleNamespace(hashString=torrent_hash, name="Show", labels=list(task["tags"]),
                                      status="stopped" if task["stopped"] else "downloading", error=3 if error else 0)
        elif isinstance(module, QbittorrentModule):
            state = "missingFiles" if error else "pausedDL" if task["stopped"] else "downloading"
            torrent = {"hash": torrent_hash, "name": "Show", "tags": ",".join(task["tags"]), "state": state}
        else:
            torrent = {"hash": torrent_hash, "name": "Show", "tags": ",".join(task["tags"]),
                       "state": 0 if task["stopped"] else 1, "is_active": 0 if task["stopped"] else 1, "complete": 0}
        return [torrent], False

    server.add_torrent.return_value = (False, []) if isinstance(module, QbittorrentModule) else False
    server.add_torrent_with_status.return_value = (None, False)
    server.get_torrents.side_effect = read_torrents
    server.get_files.side_effect = read_files
    server.set_files.side_effect = write_files
    server.start_torrents.side_effect = start
    server.torrents_set_force_start.side_effect = start
    server.resume_torrents.side_effect = start
    server.stop_torrents.side_effect = stop
    server.set_torrents_tag.side_effect = add_tags
    server.remove_torrents_tag.side_effect = remove_tags
    server.replace_torrent_labels.side_effect = replace_labels
    server.update_priorities.return_value = True
    return files, task


_SEASON = ["Show.S01E05.mkv", "Show.S01E06.mkv", "Show.S01E07.mkv"]


def _assert_never_removes_or_deselects(server) -> None:
    """
    已有任务的不变量（#7362）：不暂停、不把任何文件改为不下载、不删除任务。
    """
    for call in server.set_files.call_args_list:
        assert call.kwargs.get("priority", 1) > 0
    server.set_unwanted_files.assert_not_called()
    server.stop_torrents.assert_not_called()
    server.delete_torrents.assert_not_called()


def _assert_files_enabled(module, server, torrent_hash, file_ids) -> None:
    """
    验证只把指定文件设为下载，且只调用一次。
    """
    if isinstance(module, TransmissionModule):
        server.set_files.assert_called_once_with(torrent_hash, file_ids)
    else:
        server.set_files.assert_called_once_with(torrent_hash=torrent_hash, file_ids=file_ids, priority=1)


def _assert_task_untouched(server) -> None:
    """
    拒绝时不能对任务做任何改动：不写文件选择、不启停、不改标签。
    """
    for method in ("set_files", "start_torrents", "torrents_set_force_start", "remove_torrents_tag",
                   "set_torrents_tag", "set_torrent_tag", "replace_torrent_labels"):
        getattr(server, method).assert_not_called()
    _assert_never_removes_or_deselects(server)


def test_existing_running_task_appends_only_missing_target_files(selected_downloader) -> None:
    """
    同一季包先下 E05、再要 E06：只追加 E06，E05 的选择与 E07 的不下载状态都保持原样，运行中的任务不启停。
    """
    module, server, content, torrent_hash = selected_downloader
    files, task = existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0})

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[1] == torrent_hash
    assert result[3] == DOWNLOAD_TASK_FILES_APPENDED
    assert [files[index]["wanted"] for index in range(3)] == [True, True, False]
    _assert_files_enabled(module, server, torrent_hash, [1])
    server.start_torrents.assert_not_called()
    assert task["stopped"] is False
    _assert_never_removes_or_deselects(server)


@pytest.mark.parametrize("tags", [[], ["MOVIEPILOT", "PT站"]])
def test_paused_task_is_left_untouched(selected_downloader, tags) -> None:
    """
    暂停的任务目标还没下完：不论是谁暂停的都不恢复、不追加、不改标签，拒绝后由订阅换种子。
    """
    module, server, content, torrent_hash = selected_downloader
    files, task = existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0}, stopped=True, tags=tags)

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[1] is None
    assert ExistingTaskRejection.from_message(result[3]) is ExistingTaskRejection.PAUSED
    assert torrent_hash in result[3]
    assert files[1]["wanted"] is False and task["stopped"] is True and task["tags"] == tags
    _assert_task_untouched(server)


@pytest.mark.parametrize("target_selected,message", [
    (False, DOWNLOAD_TASK_FILES_APPENDED),
    (True, DOWNLOAD_TASK_RESUMED),
])
def test_user_specified_torrent_resumes_paused_task(selected_downloader, target_selected, message) -> None:
    """
    用户手动指定了这个种子：任务已暂停、目标没下完时，先追加目标（如需）再恢复任务；只是没下完时只恢复。
    其他文件与优先级保持原样，不取消任何选择。
    """
    module, server, content, torrent_hash = selected_downloader
    files, task = existing_task(
        module, server, torrent_hash, _SEASON, wanted={0, 1} if target_selected else {0}, completed={0}, stopped=True,
    )
    start = server.start_torrents.side_effect

    def start_after_append(*args, **kwargs):
        """
        恢复时目标必须已经追加：先恢复再追加的话，任务会先按旧的选择跑一会儿。
        """
        assert files[1]["wanted"] is True
        return start(*args, **kwargs)

    server.start_torrents.side_effect = start_after_append

    result = module.download(content, Path("/downloads"), "", episodes={6}, user_specified=True)

    assert result[1] == torrent_hash
    assert result[3] == message
    assert [files[index]["wanted"] for index in range(3)] == [True, True, False]
    assert task["stopped"] is False
    for call in server.set_files.call_args_list:
        assert call.kwargs.get("priority", 1) > 0
    server.set_unwanted_files.assert_not_called()


def test_resume_failure_fails_acceptance(selected_downloader) -> None:
    """
    恢复暂停的任务未确认时不算受理，不能让下载链记成已下载；已追加的文件不回滚，下次请求会接着补做。
    """
    module, server, content, torrent_hash = selected_downloader
    files, task = existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0}, stopped=True)
    server.start_torrents.side_effect = lambda *_args, **_kwargs: False

    result = module.download(content, Path("/downloads"), "", episodes={6}, user_specified=True)

    assert result[1] is None
    assert ExistingTaskRejection.from_message(result[3]) is ExistingTaskRejection.NOT_CONFIRMED
    assert files[1]["wanted"] is True and task["stopped"] is True


@pytest.mark.parametrize("stopped,tags,completed", [
    (True, ["MOVIEPILOT"], {1}),
    (False, [], {1}),
    (False, [], set()),
])
def test_existing_satisfied_target_is_reused_without_writes(selected_downloader, stopped, tags, completed) -> None:
    """
    目标已选中且已下完（即使任务已暂停）或正在下载时原样复用，不写入任何文件或启停。
    """
    module, server, content, torrent_hash = selected_downloader
    existing_task(module, server, torrent_hash, _SEASON, wanted={1}, completed=completed, stopped=stopped, tags=tags)

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[1] == torrent_hash
    assert result[3] == DOWNLOAD_TASK_ALREADY_EXISTS
    for method in ("set_files", "start_torrents", "torrents_set_force_start"):
        getattr(server, method).assert_not_called()
    _assert_never_removes_or_deselects(server)


def test_existing_whole_torrent_request_selects_every_video_only(selected_downloader) -> None:
    """
    不带集数的请求表示整个种子：补齐全部视频，但不去碰非视频文件（可能是用户特意取消的字幕、样片）。
    """
    module, server, content, torrent_hash = selected_downloader
    files, _task = existing_task(module, server, torrent_hash, [*_SEASON, "Show.nfo"], wanted={0}, completed={0})

    result = module.download(content, Path("/downloads"), "")

    assert result[3] == DOWNLOAD_TASK_FILES_APPENDED
    assert [files[index]["wanted"] for index in range(4)] == [True, True, True, False]
    _assert_never_removes_or_deselects(server)


def _confirmed_on(downloader, *indexes):
    """
    构造某台下载器上确认选中过 ``_SEASON`` 中指定文件的记录。
    """
    return torrent_rules.ConfirmedSelection().record(downloader, frozenset(indexes), replace=True)


@pytest.mark.parametrize("confirmed,accepted", [
    (_confirmed_on("test-downloader", 1), False),
    (_confirmed_on("test-downloader", 0, 1, 2), False),
    (_confirmed_on("test-downloader", 0), True),
    (_confirmed_on("其他下载器", 1), True),
    (torrent_rules.ConfirmedSelection(), True),
    (None, True),
])
def test_cancelled_target_video_is_never_reselected(selected_downloader, confirmed, accepted) -> None:
    """
    E06 曾由 MP 在这台下载器上确认选中、现在不再下载，只可能是别人取消的：不重新选中，拒绝后换种子。

    从未被 MP 在这台下载器上确认选中过的文件（只确认过 E05、只在别的下载器上确认过、只有升级前的历史、
    外部添加的任务）照常追加；用户手动指定种子的情况见 ``test_user_specified_torrent_reselects_cancelled_target``。
    """
    module, server, content, torrent_hash = selected_downloader
    files, _task = existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0})

    result = module.download(content, Path("/downloads"), "", episodes={6}, confirmed_selection=confirmed)

    if accepted:
        assert result[3] == DOWNLOAD_TASK_FILES_APPENDED
        assert files[1]["wanted"] is True
    else:
        assert result[1] is None
        assert ExistingTaskRejection.from_message(result[3]) is ExistingTaskRejection.FILES_CANCELLED
        assert "Show.S01E06.mkv" in result[3]
        _assert_task_untouched(server)


@pytest.mark.parametrize("user_specified,accepted", [(False, False), (True, True)])
def test_user_specified_torrent_reselects_cancelled_target(selected_downloader, user_specified, accepted) -> None:
    """
    用户手动指定种子时，按这次的明确要求重新选中之前被取消的目标；订阅等自动流程尊重之前的取消。
    """
    module, server, content, torrent_hash = selected_downloader
    files, _task = existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0})
    chain = _offline_chain(module)
    chain._existing_task_confirmed_selection = Mock(return_value=_confirmed_on("test-downloader", 1))
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()
    prepared = SimpleNamespace(
        torrent_content=content, torrent=SimpleNamespace(site_cookie=""),
        download_dir=Path("/downloads"), media=SimpleNamespace(category="TV"), site_downloader=None,
    )

    result, _message = chain._submit_prepared_download(
        prepared=prepared, context=Context(), episodes={6}, channel=None, source="Manual",
        downloader=None, userid=None, username=None, label=None, custom_words=None, governance=None,
        user_specified=user_specified,
    )

    assert (result == torrent_hash) is accepted
    assert files[1]["wanted"] is accepted
    if accepted:
        # 受理后仍以提交前读到的记录为基础合并写回，其他下载器与之前确认过的文件不会丢。
        settled = chain._settle_accepted_download.call_args.kwargs
        assert settled["previous_selection"] == _confirmed_on("test-downloader", 1)
    else:
        chain._settle_accepted_download.assert_not_called()


def test_reuse_confirms_targets_are_still_selected(selected_downloader) -> None:
    """
    目标在第一次读取时都已选中、不需要追加：读取之后别人在下载器里取消了 E06，受理前的回读要发现，
    不能按原样复用记成功。
    """
    module, server, content, torrent_hash = selected_downloader
    files, _task = existing_task(module, server, torrent_hash, _SEASON, wanted={0, 1})
    read_files = server.get_files.side_effect
    reads = []

    def deselect_after_snapshot(*args, **kwargs):
        """
        第一次读取（决策快照）之后，E06 被取消选择。
        """
        reads.append(1)
        if len(reads) == 2:
            files[1]["wanted"] = False
        return read_files(*args, **kwargs)

    server.get_files.side_effect = deselect_after_snapshot

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[1] is None
    assert ExistingTaskRejection.from_message(result[3]) is ExistingTaskRejection.NOT_CONFIRMED
    server.set_files.assert_not_called()


def test_confirmed_record_uses_downloader_file_list(selected_downloader) -> None:
    """
    受理后在任务锁内读一次下载器文件列表，确认记录按下载器自己的文件索引写入，而不是种子文件里的顺序。
    """
    module, server, content, torrent_hash = selected_downloader
    existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0})
    chain = _offline_chain(module)
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()
    prepared = SimpleNamespace(
        torrent_content=content, torrent=SimpleNamespace(site_cookie=""),
        download_dir=Path("/downloads"), media=SimpleNamespace(category="TV"), site_downloader=None,
    )

    result, _message = chain._submit_prepared_download(
        prepared=prepared, context=Context(), episodes={6}, channel=None, source="subscribe",
        downloader=None, userid=None, username=None, label=None, custom_words=None, governance=None,
    )

    assert result == torrent_hash
    settled = chain._settle_accepted_download.call_args.kwargs
    assert settled["accepted_files"] == list(enumerate(_SEASON))
    assert settled["previous_selection"] is None


def test_append_confirms_every_required_target(selected_downloader) -> None:
    """
    追加 E06 的同时，别人在下载器里取消了此前已选中的 E05：回读核对的是全部目标，E05 不在选中状态就不算受理，
    不能把 E05 记成已下载。
    """
    module, server, content, torrent_hash = selected_downloader
    files, _task = existing_task(module, server, torrent_hash, _SEASON, wanted={0})
    write_files = server.set_files.side_effect

    def write_and_deselect(*args, **kwargs):
        """
        追加文件期间 E05 被别人取消选择。
        """
        files[0]["wanted"] = False
        return write_files(*args, **kwargs)

    server.set_files.side_effect = write_and_deselect

    result = module.download(content, Path("/downloads"), "", episodes={5, 6})

    assert result[1] is None
    assert ExistingTaskRejection.from_message(result[3]) is ExistingTaskRejection.NOT_CONFIRMED
    _assert_never_removes_or_deselects(server)


@pytest.mark.parametrize("failure,rejection", [
    ("unmatched", ExistingTaskRejection.EPISODES_NOT_FOUND),
    ("unconfirmed", ExistingTaskRejection.NOT_CONFIRMED),
    ("no_metadata", ExistingTaskRejection.FILES_UNAVAILABLE),
    ("no_metadata_whole", ExistingTaskRejection.FILES_UNAVAILABLE),
    ("error", ExistingTaskRejection.TASK_ERROR),
])
def test_existing_task_failure_never_settles_success(selected_downloader, failure, rejection) -> None:
    """
    目标未匹配、写入未生效、读不到文件列表（不论是否带集数）或任务出错时都不返回 hash。

    已有任务失败时绝不能套用新任务的清理逻辑（#7419），否则会删掉正在做种的整包。
    """
    module, server, content, torrent_hash = selected_downloader
    if failure == "error" and isinstance(module, RtorrentModule):
        pytest.skip("rTorrent 没有可靠的任务错误状态")
    existing_task(module, server, torrent_hash, _SEASON, wanted={0}, tags=["已整理"], error=failure == "error")
    if failure == "unconfirmed":
        server.set_files.side_effect = None
        server.set_files.return_value = True
    elif failure.startswith("no_metadata"):
        server.get_files.side_effect = None
        server.get_files.return_value = []

    episodes = None if failure == "no_metadata_whole" else {9 if failure == "unmatched" else 6}
    result = module.download(content, Path("/downloads"), "", episodes=episodes)

    assert result[1] is None
    assert ExistingTaskRejection.from_message(result[3]) is rejection
    assert torrent_hash in result[3]
    if failure in ("unmatched", "no_metadata", "no_metadata_whole", "error"):
        _assert_task_untouched(server)
    _assert_never_removes_or_deselects(server)


def test_existing_task_clears_archive_tag_after_acceptance(selected_downloader) -> None:
    """
    追加受理后移除"已整理"，追加的文件下载完成后才能重新进入下载器整理。
    """
    module, server, content, torrent_hash = selected_downloader
    _files, task = existing_task(
        module, server, torrent_hash, _SEASON, wanted={0}, completed={0}, tags=["MOVIEPILOT", "已整理"],
    )

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[3] == DOWNLOAD_TASK_FILES_APPENDED
    assert task["tags"] == ["MOVIEPILOT"]


def test_archive_tag_removal_failure_fails_acceptance(selected_downloader) -> None:
    """
    "已整理"移除未确认时不算受理：标签留着的话，追加的文件下载完成后不会自动整理。
    """
    module, server, content, torrent_hash = selected_downloader
    existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0}, tags=["MOVIEPILOT", "已整理"])
    server.remove_torrents_tag.side_effect = None
    server.remove_torrents_tag.return_value = False
    server.replace_torrent_labels.side_effect = None
    server.replace_torrent_labels.return_value = False

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[1] is None
    assert ExistingTaskRejection.from_message(result[3]) is ExistingTaskRejection.NOT_CONFIRMED


@pytest.mark.parametrize("failure", ["write", "read_back"])
def test_torrent_tag_must_be_confirmed(selected_downloader, failure) -> None:
    """
    外部添加、没有系统种子标签的任务：下载器整理轮询只扫描带这个标签的任务，补写失败或回读不到时不算受理，
    否则追加的集下载完成后不会被自动整理，订阅却已记成下载中。
    """
    module, server, content, torrent_hash = selected_downloader
    files, task = existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0}, tags=["PT站"])
    if failure == "write":
        server.set_torrents_tag.side_effect = lambda *_args, **_kwargs: False
        server.replace_torrent_labels.side_effect = lambda *_args, **_kwargs: False
    else:
        server.set_torrents_tag.side_effect = lambda *_args, **_kwargs: True
        server.replace_torrent_labels.side_effect = lambda *_args, **_kwargs: True

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[1] is None
    assert ExistingTaskRejection.from_message(result[3]) is ExistingTaskRejection.NOT_CONFIRMED
    assert "MOVIEPILOT" in result[3]
    assert task["tags"] == ["PT站"]
    # 已追加的文件不回滚，下一轮按原样复用重试补写标签。
    assert files[1]["wanted"] is True


def test_transmission_clears_only_archive_label_without_torrent_tag(selected_downloader, monkeypatch) -> None:
    """
    未配置系统种子标签且只剩"已整理"时也要清空：旧实现把移除嵌在系统标签判断里，永远清不掉。
    """
    module, server, content, torrent_hash = selected_downloader
    if not isinstance(module, TransmissionModule):
        pytest.skip("Transmission 标签整体写回")
    original = downloader_base.get_runtime_setting
    monkeypatch.setattr(
        downloader_base, "get_runtime_setting",
        lambda key, *args: "" if key == "TORRENT_TAG" else original(key, *args),
    )
    existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0}, tags=["已整理"])

    module.download(content, Path("/downloads"), "", episodes={6})

    server.replace_torrent_labels.assert_called_once_with(ids=torrent_hash, labels=[])


@pytest.mark.parametrize("appended", [True, False])
def test_transfer_completed_rolls_back_tag_when_files_were_appended(selected_downloader, appended) -> None:
    """
    E05 整理完打"已整理"的同时 E06 被追加：复核到仍有已选中未完成的文件就撤回标签，
    否则 E06 下载完成后会被下载器整理永久跳过；没有追加时保持原有打标签行为。
    """
    module, server, _content, torrent_hash = selected_downloader
    _files, task = existing_task(
        module, server, torrent_hash, _SEASON, wanted={0, 1} if appended else {0}, completed={0}, tags=["MOVIEPILOT"],
    )

    module.transfer_completed(torrent_hash)

    assert task["tags"] == (["MOVIEPILOT"] if appended else ["MOVIEPILOT", "已整理"])


def test_transfer_completed_rolls_back_tag_when_file_state_is_unreadable(selected_downloader) -> None:
    """
    打标签后读不到文件状态时无法确认，撤回标签：只会让下一轮整理重新检查（已整理过的文件会跳过）。
    """
    module, server, _content, torrent_hash = selected_downloader
    _files, task = existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0}, tags=["MOVIEPILOT"])
    server.get_files.side_effect = None
    server.get_files.return_value = None

    module.transfer_completed(torrent_hash)

    assert task["tags"] == ["MOVIEPILOT"]


def test_transfer_completed_never_overwrites_labels_when_read_fails(selected_downloader) -> None:
    """
    读不到当前标签时不写"已整理"：旧实现把读取失败当成没有标签再整体覆盖，会清掉 PT 站标签。
    """
    module, server, _content, torrent_hash = selected_downloader
    if isinstance(module, QbittorrentModule):
        pytest.skip("qBittorrent 单独追加标签，不需要先读取")
    _files, task = existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0}, tags=["PT站", "MOVIEPILOT"])
    server.get_torrents.side_effect = None
    server.get_torrents.return_value = ([], True)
    # rTorrent 客户端在合并模式下读取 d.custom1 失败时返回 False，不写入。
    server.set_torrents_tag.side_effect = None
    server.set_torrents_tag.return_value = False

    module.transfer_completed(torrent_hash)

    assert task["tags"] == ["PT站", "MOVIEPILOT"]
    server.replace_torrent_labels.assert_not_called()
    server.set_torrent_tag.assert_not_called()


@pytest.mark.parametrize("failed_reads,expected", [
    (0, ["PT站", "新标签"]),
    (1, ["PT站", "新标签"]),
    (3, ["PT站", "已整理", "新标签"]),
])
def test_transmission_rollback_rereads_labels(selected_downloader, monkeypatch, failed_reads, expected) -> None:
    """
    Transmission 只能整体覆盖标签：写入"已整理"后复核期间别处新加了标签，撤回时基于重新读到的标签计算，保留新标签。
    读取失败时短暂重试；一直读不到就不撤回，绝不用写入前的旧列表覆盖（那会清掉刚加的标签）。
    """
    module, server, _content, torrent_hash = selected_downloader
    if not isinstance(module, TransmissionModule):
        pytest.skip("只有 Transmission 需要整体覆盖标签")
    monkeypatch.setattr(transmission_module, "_LABEL_REREAD_INTERVAL", 0)
    _files, task = existing_task(module, server, torrent_hash, _SEASON, wanted={0, 1}, completed={0}, tags=["PT站"])
    read_files = server.get_files.side_effect
    read_torrents = server.get_torrents.side_effect
    failures = {"left": failed_reads}

    def flaky_torrents(*args, **kwargs):
        """
        复核之后的标签读取先失败指定次数。
        """
        if failures["left"] > 0:
            failures["left"] -= 1
            return [], True
        return read_torrents(*args, **kwargs)

    def tag_during_review(*args, **kwargs):
        """
        复核逐文件状态时，别处给任务加了新标签；之后的标签读取按需失败。
        """
        task["tags"].append("新标签")
        server.get_torrents.side_effect = flaky_torrents
        return read_files(*args, **kwargs)

    server.get_files.side_effect = tag_during_review

    module.transfer_completed(torrent_hash)

    assert task["tags"] == expected


@pytest.mark.parametrize("existing,written", [
    (["MOVIEPILOT", "TV"], None),
    (["MOVIEPILOT"], ["TV"]),
    (["PT站"], ["MOVIEPILOT", "TV"]),
])
def test_multiple_torrent_tags_are_checked_one_by_one(selected_downloader, monkeypatch, existing, written) -> None:
    """
    TORRENT_TAG 可以配置多个逗号分隔的标签，整理轮询要求全部带上：只补缺少的，回读确认全部都在；
    已经全部带上时不写入，也不能因为把整串配置当成一个标签比较而误判失败、让订阅换源重复下载。
    """
    module, server, content, torrent_hash = selected_downloader
    original = downloader_base.get_runtime_setting
    monkeypatch.setattr(
        downloader_base, "get_runtime_setting",
        lambda key, *args: "MOVIEPILOT, TV" if key == "TORRENT_TAG" else original(key, *args),
    )
    _files, task = existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0}, tags=existing)

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[3] == DOWNLOAD_TASK_FILES_APPENDED
    assert set(task["tags"]) == {*existing, "MOVIEPILOT", "TV"}
    if written is None:
        server.set_torrents_tag.assert_not_called()
        server.replace_torrent_labels.assert_not_called()
    elif isinstance(module, TransmissionModule):
        server.replace_torrent_labels.assert_called_once_with(ids=torrent_hash, labels=[*existing, *written])
    else:
        server.set_torrents_tag.assert_called_once_with(ids=torrent_hash, tags=written)


def _transfer_torrent(torrent_hash, *, selected_size, size=126):
    """
    构造整理轮询查到的已完成任务。
    """
    return DownloaderTorrent(downloader="test-downloader", hash=torrent_hash, size=size, selected_size=selected_size)


def test_partial_task_reports_incomplete_files_for_transfer(selected_downloader) -> None:
    """
    E05 已下完，E06 下到一半后被取消选择（残缺文件仍在磁盘上），E07 从未下载：任务按选中部分已完成进入整理轮询，
    同时附上下载器报告未下载完成的 E06、E07，整理时跳过，不把残缺视频当成正常剧集入库。
    """
    module, server, _content, torrent_hash = selected_downloader
    existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0})

    (admitted,) = module._admit_transfer_torrent(server, _transfer_torrent(torrent_hash, selected_size=42))

    assert admitted.incomplete_files == ["Show.S01E06.mkv", "Show.S01E07.mkv"]


def test_fully_selected_task_needs_no_file_check(selected_downloader) -> None:
    """
    选中大小等于总大小（整包都下完）时不需要逐文件确认，不多查一次文件列表。
    """
    module, server, _content, torrent_hash = selected_downloader

    (admitted,) = module._admit_transfer_torrent(server, _transfer_torrent(torrent_hash, selected_size=126))

    assert admitted.incomplete_files is None
    server.get_files.assert_not_called()


def test_partial_task_with_unreadable_files_waits_for_next_round(selected_downloader) -> None:
    """
    只选中部分文件、却读不到逐文件状态时本轮不交给整理：宁可晚整理，也不整理残缺文件。
    """
    module, server, _content, torrent_hash = selected_downloader
    server.get_files.return_value = None

    assert module._admit_transfer_torrent(server, _transfer_torrent(torrent_hash, selected_size=42)) == []


def test_rtorrent_pending_target_refreshes_priorities_on_retry(selected_downloader) -> None:
    """
    rTorrent 上次写入中途失败、重试时目标读回已是选中：仍要刷新优先级，否则写入永远不生效。
    """
    module, server, content, torrent_hash = selected_downloader
    if not isinstance(module, RtorrentModule):
        pytest.skip("只有 rTorrent 需要显式刷新文件优先级")
    existing_task(module, server, torrent_hash, _SEASON, wanted={1})

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[3] == DOWNLOAD_TASK_ALREADY_EXISTS
    server.update_priorities.assert_called_once_with(torrent_hash)


def test_transferred_tag_written_after_snapshot_is_still_removed(selected_downloader) -> None:
    """
    受理前读取的标签里没有"已整理"，追加期间却被写上（MP 进程外或锁管不到的写入）：移除前重新读取标签，
    仍能把它移除，追加的文件下载完成后才会进入下载器整理。
    """
    module, server, content, torrent_hash = selected_downloader
    _files, task = existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0}, tags=["MOVIEPILOT"])
    write_files = server.set_files.side_effect

    def write_and_tag(*args, **kwargs):
        """
        追加文件的同时模拟整理回调写入"已整理"。
        """
        task["tags"].append("已整理")
        return write_files(*args, **kwargs)

    server.set_files.side_effect = write_and_tag

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[1] == torrent_hash
    assert result[3] == DOWNLOAD_TASK_FILES_APPENDED
    assert "已整理" not in task["tags"]


def test_transferred_tag_removal_must_be_read_back(selected_downloader) -> None:
    """
    下载器接口返回成功只代表请求已发出：移除"已整理"后回读仍在，按未确认失败，不能让订阅记成已下载。
    """
    module, server, content, torrent_hash = selected_downloader
    _files, task = existing_task(
        module, server, torrent_hash, _SEASON, wanted={0}, completed={0}, tags=["MOVIEPILOT", "已整理"],
    )
    server.remove_torrents_tag.side_effect = lambda *_args, **_kwargs: True
    server.replace_torrent_labels.side_effect = lambda *_args, **_kwargs: True

    result = module.download(content, Path("/downloads"), "", episodes={5})

    assert result[1] is None
    assert ExistingTaskRejection.from_message(result[3]) is ExistingTaskRejection.NOT_CONFIRMED
    assert "已整理" in task["tags"]


def test_rtorrent_finds_existing_task_before_adding(selected_downloader) -> None:
    """
    rTorrent 的 load.raw 先返回成功、再在内部判重并静默丢弃重复种子，不会出现带临时标签的新任务：
    添加前按 hash 查到已有任务就直接走已有任务流程，不发起添加。
    """
    module, server, content, torrent_hash = selected_downloader
    if not isinstance(module, RtorrentModule):
        pytest.skip("只有 rTorrent 的添加接口异步判重")
    _files, task = existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0})
    server.add_torrent.return_value = True
    server.get_torrent_id_by_tag.return_value = None

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[1] == torrent_hash
    assert result[3] == DOWNLOAD_TASK_FILES_APPENDED
    server.add_torrent.assert_not_called()
    server.get_torrent_id_by_tag.assert_not_called()


def test_rtorrent_task_added_after_precheck_falls_back_to_existing_task(selected_downloader) -> None:
    """
    添加前没查到、添加后又找不到临时标签：同一 hash 的任务可能刚被别处加入，本次种子被判重丢弃，
    再按 hash 查一次并按已有任务处理。
    """
    module, server, content, torrent_hash = selected_downloader
    if not isinstance(module, RtorrentModule):
        pytest.skip("只有 rTorrent 的添加接口异步判重")
    _files, _task = existing_task(module, server, torrent_hash, _SEASON, wanted={0}, completed={0})
    read_torrents = server.get_torrents.side_effect
    calls = []

    def appear_after_first_query(*args, **kwargs):
        """
        第一次查询（添加前）时任务还不存在。
        """
        calls.append(args or kwargs)
        return ([], False) if len(calls) == 1 else read_torrents(*args, **kwargs)

    server.get_torrents.side_effect = appear_after_first_query
    server.add_torrent.return_value = True
    server.get_torrent_id_by_tag.return_value = None

    result = module.download(content, Path("/downloads"), "", episodes={6})

    assert result[1] == torrent_hash
    assert result[3] == DOWNLOAD_TASK_FILES_APPENDED
    server.add_torrent.assert_called_once()


def test_rtorrent_own_task_found_by_hash_is_still_a_new_task(selected_downloader, monkeypatch) -> None:
    """
    按临时标签轮询没读到、按 hash 查到的任务却带着本次临时标签：是本次新建的任务，按新任务选集，
    不能当成已有任务（否则通知、确认记录都会按复用处理）。
    """
    module, server, content, torrent_hash = selected_downloader
    if not isinstance(module, RtorrentModule):
        pytest.skip("只有 rTorrent 的添加接口异步判重")
    monkeypatch.setattr("app.modules.rtorrent.text_tools.random_string", lambda _length: "tmp-tag")
    _set_files(module, server, ["Show.S01E05.mkv", "Show.S01E06.mkv"])
    calls = []

    def appear_after_add(*args, **kwargs):
        """
        添加前下载器里没有该任务，添加后出现带临时标签的新任务。
        """
        calls.append(args or kwargs)
        if len(calls) == 1:
            return [], False
        return [{"hash": torrent_hash.upper(), "tags": "tmp-tag,MOVIEPILOT", "state": 0, "is_active": 0}], False

    server.get_torrents.side_effect = appear_after_add
    server.add_torrent.return_value = True
    server.get_torrent_id_by_tag.return_value = None

    result = module.download(content, Path("/downloads"), "", episodes={5})

    assert result[1] == torrent_hash.upper()
    assert result[3].startswith("添加下载成功")
    server.remove_torrents_tag.assert_called_once_with(ids=torrent_hash.upper(), tag=["tmp-tag"])
    server.set_files.assert_any_call(torrent_hash=torrent_hash.upper(), file_ids=[1], priority=0)


@pytest.mark.parametrize("state,active,checking,expected", [
    (0, 0, None, torrent_rules.ExistingTaskState.PAUSED),
    (1, 1, None, torrent_rules.ExistingTaskState.RUNNING),
    (1, 0, True, torrent_rules.ExistingTaskState.RUNNING),
    (1, 0, False, torrent_rules.ExistingTaskState.PAUSED),
    (1, 0, None, torrent_rules.ExistingTaskState.UNKNOWN),
])
def test_rtorrent_hash_checking_task_is_not_treated_as_paused(state, active, checking, expected) -> None:
    """
    rTorrent 启动后校验数据期间任务不活动（state=1、is_active=0），校验完会自动开始，按运行处理；
    不在校验的不活动任务是被 d.pause 暂停的；读不到校验状态时不猜。
    """
    module = RtorrentModule.__new__(RtorrentModule)
    server = Mock()
    server.get_torrents.return_value = ([{"hash": "ABC", "state": state, "is_active": active}], False)
    server.is_hash_checking.return_value = checking

    assert module._existing_task_state(server, "abc") is expected
    if state == 0 or active == 1:
        server.is_hash_checking.assert_not_called()


def test_stop_and_start_leave_tags_alone(selected_downloader) -> None:
    """
    暂停与开始只改启停状态，不读写任务标签。
    """
    module, server, _content, torrent_hash = selected_downloader
    _files, task = existing_task(module, server, torrent_hash, _SEASON, tags=["MOVIEPILOT"])

    module.stop_torrents(torrent_hash)
    assert task == {"stopped": True, "tags": ["MOVIEPILOT"]}
    module.start_torrents(torrent_hash)
    assert task == {"stopped": False, "tags": ["MOVIEPILOT"]}
    for method in ("set_torrents_tag", "remove_torrents_tag", "replace_torrent_labels"):
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
    chain = _offline_chain(module)
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
    chain = _offline_chain(module)
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
