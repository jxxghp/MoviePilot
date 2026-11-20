"""
真实下载器模块与下载链的选集、身份确认边界回归。
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from torrentool.bencode import Bencode

import app.chain.download.submission as submission_module
import app.modules.transmission.transmission as transmission_client
from app.chain.download.facade import DownloadChain
from app.chain.download.failure import (
    DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS,
    DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS,
    DownloadFailureOwner,
)
from app.domain import torrent as torrent_rules
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.domain.torrent import ConfirmedSelection
from app.modules.qbittorrent import QbittorrentModule
from app.modules.transmission import TransmissionModule
from app.modules.transmission.transmission import Transmission
from app.schemas.download import DOWNLOAD_TASK_FILES_APPENDED, DownloadTaskAction, ExistingTaskRejection
from app.schemas.message import Message
from app.schemas.types import MediaType
from tests.test_episode_file_selection import existing_task

_CUSTOM_WORDS = (
    "A.Will.Eternal.S04 => 一念永恒{[tmdbid=107371;type=tv]}S01 "
    "&& S01 <> 2160p >> EP+165"
)
_FILES = ["A.Will.Eternal.S04E05.2160p.mkv", "A.Will.Eternal.S04E06.2160p.mkv"]


def _torrent(version="v1"):
    """
    构造只在内存解析的合法 v1/v2 种子和下载器可能使用的全部身份。
    """
    info = {"name": "Same.Show.S01", "piece length": 16384}
    if version in {"v1", "hybrid"}:
        info.update({"pieces": b"x" * 20, "files": [
            {"length": 21, "path": [name]} for name in _FILES
        ]})
    if version in {"v2", "hybrid"}:
        info.update({"meta version": 2, "file tree": {
            name: {"": {"length": 21, "pieces root": b"\xff" * 32}} for name in _FILES
        }})
    hashes = []
    if version in {"v1", "hybrid"}:
        hashes.append(hashlib.sha1(Bencode.encode(info)).hexdigest())
    if version in {"v2", "hybrid"}:
        digest = hashlib.sha256(Bencode.encode(info)).hexdigest()
        hashes.extend((digest, digest[:40]))
    return Bencode.encode({"info": info}), hashes


@pytest.fixture(params=[QbittorrentModule, TransmissionModule], ids=["qbittorrent", "transmission"])
def downloader(request):
    """
    使用真实模块实现，仅替换外部下载器客户端。
    """
    content, hashes = _torrent()
    server = Mock()
    server.add_torrent.return_value = (True, [hashes[0]])
    server.add_torrent_with_status.return_value = (SimpleNamespace(hashString=hashes[0]), False)
    server.get_content_layout.return_value = "Original"
    server.is_force_resume.return_value = False
    server.get_files.side_effect = lambda *_args, **_kwargs: (
        [{"id": index, "name": f"Folder.S09E99/{name}"} for index, name in enumerate(_FILES)]
        if request.param is QbittorrentModule else
        [SimpleNamespace(id=index, name=f"Folder.S09E99/{name}") for index, name in enumerate(_FILES)]
    )
    for method in ("set_files", "set_unwanted_files", "start_torrents", "stop_torrents"):
        getattr(server, method).return_value = True
    server.ensure_torrent_state.side_effect = (
        lambda torrent_hash, *, paused:
        server.stop_torrents(torrent_hash) if paused else server.start_torrents(torrent_hash)
    )
    module = request.param.__new__(request.param)
    module.get_instance = Mock(return_value=server)
    module.normalize_path = Mock(side_effect=lambda path, _name: path)
    module.get_default_config_name = Mock(return_value="test-downloader")
    return module, server, content, hashes[0]


def _submit(chain, content, episodes, custom_words):
    """
    从下载链实际模块调用包装器提交，捕获成功或失败结算边界。
    """
    prepared = SimpleNamespace(
        torrent_content=content, torrent=SimpleNamespace(site_cookie=""),
        download_dir=Path("/downloads"), media=SimpleNamespace(category="TV"), site_downloader=None,
    )
    if not hasattr(chain, "download_history_repository"):
        # 没有下载历史：已有任务视为外部添加，没有"MP 确认选中过"的记录。
        chain.download_history_repository = Mock()
        chain.download_history_repository.get_by_hashes.return_value = {}
    return chain._submit_prepared_download(
        prepared=prepared, context=Context(), episodes=episodes, channel=None, source="subscribe",
        downloader="test-downloader", userid=None, username=None, label=None,
        custom_words=custom_words, governance=None,
    )


def test_subscription_mapping_reaches_real_downloader_selection(downloader):
    """
    S04E05 映射为 E170 时，仅下载其物理文件且成功结算。
    """
    module, server, content, torrent_hash = downloader
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: getattr(module, method)(**kwargs)
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()

    result, _message = _submit(chain, content, {170}, _CUSTOM_WORDS)

    assert result == torrent_hash
    if isinstance(module, QbittorrentModule):
        assert server.set_files.call_args_list[0].kwargs == {
            "torrent_hash": torrent_hash, "file_ids": [1], "priority": 0,
        }
        assert server.set_files.call_args_list[1].kwargs["file_ids"] == [0]
    else:
        server.set_files.assert_called_once_with(torrent_hash, [0])
        server.set_unwanted_files.assert_called_once_with(torrent_hash, [1])
    server.start_torrents.assert_called_once_with(torrent_hash)
    chain._settle_accepted_download.assert_called_once()
    chain._record_rejected_download.assert_not_called()


def test_single_video_file_selection_accepts_episode_label_mismatch(downloader):
    """
    单集种子只有一个视频文件时，累计集号与目标季集不一致也应选中该文件。
    """
    module, server, _content, torrent_hash = downloader
    server.get_files.side_effect = None
    if isinstance(module, QbittorrentModule):
        server.get_files.return_value = [
            {"id": 0, "name": "Show.S03E54.mp4"},
            {"id": 1, "name": "Show.S03E54.ass"},
        ]
    else:
        server.get_files.return_value = [
            SimpleNamespace(id=0, name="Show.S03E54.mp4"),
            SimpleNamespace(id=1, name="Show.S03E54.ass"),
        ]

    selected, message = module._select_download_files(server, torrent_hash, {6}, None)

    assert selected is True
    assert "未找到目标集数" not in message
    if isinstance(module, QbittorrentModule):
        assert server.set_files.call_args_list[0].kwargs == {
            "torrent_hash": torrent_hash, "file_ids": [1], "priority": 0,
        }
        assert server.set_files.call_args_list[1].kwargs == {
            "torrent_hash": torrent_hash, "file_ids": [0], "priority": 1,
        }
    else:
        server.set_files.assert_called_once_with(torrent_hash, [0])
        server.set_unwanted_files.assert_called_once_with(torrent_hash, [1])


def test_paused_magnet_without_metadata_resumes_to_fetch_then_repauses(downloader):
    """
    暂停的磁力无元数据时，先启动取元数据、再暂停选集，而不是直接报获取种子文件失败。

    复现 #7347：暂停任务不建立 peer/DHT 连接，第一次读文件为空；启动后元数据
    到达，重新暂停后再按优先级选集、启动。
    """
    module, server, _content, torrent_hash = downloader
    server.get_files.side_effect = _files_after_start(server, module, torrent_hash)

    selected, message = module._select_download_files(server, torrent_hash, {170}, _CUSTOM_WORDS)

    assert selected is True
    assert "已选择集数" in message or message == "添加下载任务成功"
    # 取元数据启动一次，选集完成后再启动一次；重新暂停发生在两次启动之间、设优先级之前。
    assert server.start_torrents.call_count == 2
    if isinstance(module, QbittorrentModule):
        assert server.stop_torrents.call_count == 1
        assert server.set_files.call_args_list[0].kwargs == {
            "torrent_hash": torrent_hash, "file_ids": [1], "priority": 0,
        }
        assert server.set_files.call_args_list[1].kwargs == {
            "torrent_hash": torrent_hash, "file_ids": [0], "priority": 1,
        }
    else:
        assert server.stop_torrents.call_count == 2
        server.set_files.assert_called_once_with(torrent_hash, [0])
        server.set_unwanted_files.assert_called_once_with(torrent_hash, [1])
    calls = [str(call) for call in server.mock_calls]
    resume = next(index for index, call in enumerate(calls) if "start_torrents" in call)
    repause = next(index for index, call in enumerate(calls) if index > resume and "stop_torrents" in call)
    set_files = next(index for index, call in enumerate(calls) if "set_files" in call)
    final_start = max(index for index, call in enumerate(calls) if "start_torrents" in call)
    assert resume < repause < set_files < final_start


def _files_after_start(server, module, torrent_hash):
    """
    模拟暂停磁力在启动前无元数据、启动后才有文件列表。

    :param server: 被替换的外部下载器客户端
    :param module: 当前参数化的下载器模块
    :param torrent_hash: 本次选集的任务哈希
    """
    if isinstance(module, QbittorrentModule):
        files = [{"id": index, "name": f"Folder.S09E99/{name}"} for index, name in enumerate(_FILES)]
    else:
        files = [SimpleNamespace(id=index, name=f"Folder.S09E99/{name}") for index, name in enumerate(_FILES)]

    def read_files(*_args, **_kwargs):
        """
        启动前返回空列表模拟无元数据，启动后返回真实文件列表。
        """
        return list(files) if server.start_torrents.called else []

    return read_files


def test_metadata_wait_failure_restores_paused_state(downloader):
    """
    启动后仍无元数据时，任务应恢复暂停并报获取种子文件失败，不能留在运行状态。
    """
    module, server, _content, torrent_hash = downloader
    server.get_files.side_effect = None
    server.get_files.return_value = []

    selected, message = module._select_download_files(server, torrent_hash, {170}, _CUSTOM_WORDS)

    assert selected is False
    assert "获取种子文件失败" in message and "保持暂停" in message
    server.start_torrents.assert_called_once_with(torrent_hash)
    if isinstance(module, QbittorrentModule):
        server.stop_torrents.assert_called_once_with(torrent_hash)
    else:
        assert server.stop_torrents.call_count == 2
    server.set_files.assert_not_called()
    server.set_unwanted_files.assert_not_called()
    # 取元数据等待次数有界：第一次 5×1s，取元数据时 10×1s。
    assert server.get_files.call_count == 2
    assert server.get_files.call_args_list[1].kwargs == {"retry": 10, "interval": 1}
    if isinstance(module, QbittorrentModule):
        assert server.get_files.call_args_list[0].kwargs == {"retry": 5, "interval": 1}
    else:
        assert server.get_files.call_args_list[0].kwargs == {}


def test_repause_failure_reports_unconfirmed_state(downloader):
    """
    取元数据后无法重新暂停时，后续失败不得再称“保持暂停”，必须报状态未确认。
    """
    module, server, _content, torrent_hash = downloader
    server.get_files.side_effect = _files_after_start(server, module, torrent_hash)
    if isinstance(module, QbittorrentModule):
        server.stop_torrents.return_value = False
    else:
        server.stop_torrents.side_effect = [True, False]

    selected, message = module._select_download_files(server, torrent_hash, {999}, _CUSTOM_WORDS)

    assert selected is False
    assert "未找到目标集数" in message
    assert "核实" in message
    assert "保持暂停" not in message


def test_metadata_timeout_repause_failure_reports_unconfirmed_state(downloader):
    """
    超时仍无元数据且恢复暂停失败时，不得报“保持暂停”，必须报状态未确认。
    """
    module, server, _content, torrent_hash = downloader
    server.get_files.side_effect = None
    server.get_files.return_value = []
    if isinstance(module, QbittorrentModule):
        server.stop_torrents.return_value = False
    else:
        server.stop_torrents.side_effect = [True, False]

    selected, message = module._select_download_files(server, torrent_hash, {170}, _CUSTOM_WORDS)

    assert selected is False
    assert "核实" in message
    assert "保持暂停" not in message
    assert "获取种子文件失败" not in message


def test_metadata_resume_uses_plain_start_under_force_resume(downloader):
    """
    开启强制恢复时，取元数据仍用普通启动，最终启动才用强制恢复；取元数据不等同于下载。
    """
    module, server, _content, torrent_hash = downloader
    if not isinstance(module, QbittorrentModule):
        pytest.skip("qBittorrent 强制恢复")
    server.is_force_resume.return_value = True
    server.get_files.side_effect = _files_after_start(server, module, torrent_hash)

    selected, _message = module._select_download_files(server, torrent_hash, {170}, _CUSTOM_WORDS)

    assert selected is True
    server.start_torrents.assert_called_once_with(torrent_hash)
    server.torrents_set_force_start.assert_called_once_with(torrent_hash)


def test_metadata_resume_composes_with_single_video_fallback(downloader):
    """
    取元数据成功后，#7345 的单视频兜底仍生效：单集种子只有一个视频文件时选中它。
    """
    module, server, _content, torrent_hash = downloader
    server.get_files.side_effect = _single_video_after_start(server, module, torrent_hash)

    selected, message = module._select_download_files(server, torrent_hash, {6}, None)

    assert selected is True
    assert "未找到目标集数" not in message
    if isinstance(module, QbittorrentModule):
        assert server.set_files.call_args_list[0].kwargs == {
            "torrent_hash": torrent_hash, "file_ids": [1], "priority": 0,
        }
        assert server.set_files.call_args_list[1].kwargs == {
            "torrent_hash": torrent_hash, "file_ids": [0], "priority": 1,
        }
    else:
        server.set_files.assert_called_once_with(torrent_hash, [0])
        server.set_unwanted_files.assert_called_once_with(torrent_hash, [1])


def _single_video_after_start(server, module, torrent_hash):
    """
    模拟单视频种子在启动前无元数据、启动后才有文件列表。

    :param server: 被替换的外部下载器客户端
    :param module: 当前参数化的下载器模块
    :param torrent_hash: 本次选集的任务哈希
    """
    if isinstance(module, QbittorrentModule):
        files = [
            {"id": 0, "name": "Show.S03E54.mp4"},
            {"id": 1, "name": "Show.S03E54.ass"},
        ]
    else:
        files = [
            SimpleNamespace(id=0, name="Show.S03E54.mp4"),
            SimpleNamespace(id=1, name="Show.S03E54.ass"),
        ]

    def read_files(*_args, **_kwargs):
        """
        启动前返回空列表模拟无元数据，启动后返回真实文件列表。
        """
        return list(files) if server.start_torrents.called else []

    return read_files


def test_existing_magnet_without_metadata_fails_without_state_change(downloader):
    """
    带集数重试遇到尚无元数据的已有磁力：不为取元数据而启停它，也不返回成功 hash。

    启动后下载器会默认下载全部文件，与"只要目标集"冲突；#7348 的取元数据流程只用于新任务。
    """
    module, server, content, torrent_hash = downloader
    server.get_files.side_effect = _files_after_start(server, module, torrent_hash)
    _report_existing_task(server, module, torrent_hash)

    result = module.download(
        content, Path("/downloads"), "", episodes={170},
        custom_words=_CUSTOM_WORDS, downloader="test-downloader",
    )

    assert result[1] is None
    assert torrent_hash in result[3] and "元数据" in result[3]
    server.start_torrents.assert_not_called()
    server.stop_torrents.assert_not_called()
    server.set_files.assert_not_called()


@pytest.mark.parametrize("file_count", [1, 2])
def test_transmission_all_files_wanted_starts_without_unwanted_rpc(downloader, file_count) -> None:
    """
    单集和多集文件全部命中时，经真实客户端封装仅发送需要下载的索引后启动。
    """
    module, server, content, torrent_hash = downloader
    if not isinstance(module, TransmissionModule):
        pytest.skip("Transmission 空文件数组的 RPC 语义")
    list_files = server.get_files.side_effect
    server.get_files.side_effect = lambda *args: list_files(*args)[:file_count]
    client = Transmission.__new__(Transmission)
    client.trc = Mock()
    server.set_files = Mock(wraps=client.set_files)
    server.set_unwanted_files = Mock(wraps=client.set_unwanted_files)

    def start_selected_task(_torrent_hash) -> bool:
        """
        启动之前确认所有目标文件已选中，且未发送覆盖全部文件的不下载请求。
        """
        client.trc.change_torrent.assert_called_once_with(
            ids=torrent_hash, files_wanted=list(range(file_count)),
        )
        return True

    server.start_torrents.side_effect = start_selected_task
    result = module.download(
        content, Path("/downloads"), "", episodes=set(range(170, 170 + file_count)),
        custom_words=_CUSTOM_WORDS,
    )

    assert result[1] == torrent_hash
    client.trc.change_torrent.assert_called_once_with(ids=torrent_hash, files_wanted=list(range(file_count)))
    server.start_torrents.assert_called_once_with(torrent_hash)


def _use_transmission_rpc(module, server) -> Transmission:
    """
    保留真实客户端的启停逻辑，仅替换 RPC 和种子文件读取边界。
    """
    client = Transmission.__new__(Transmission)
    client.trc = Mock()
    client.add_torrent_with_status = server.add_torrent_with_status
    client.get_files = server.get_files
    module.get_instance.return_value = client
    return client


@pytest.fixture
def transmission_clock(monkeypatch):
    """
    在真实模块和客户端的联动测试中推进虚拟轮询时间。
    """
    clock = SimpleNamespace(now=0.0)

    def advance(seconds: float) -> None:
        """
        按客户端要求推进等待时间。
        """
        clock.now += seconds

    monkeypatch.setattr(transmission_client, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=advance))
    return clock


def test_transmission_new_paused_task_starts_without_stop_rpc(downloader) -> None:
    """
    已暂停的新任务无需再次停止，防止停止请求覆盖选集后的启动请求。
    """
    module, server, content, torrent_hash = downloader
    if not isinstance(module, TransmissionModule):
        pytest.skip("Transmission 的异步启停状态确认")
    client = _use_transmission_rpc(module, server)
    state = {"status": "stopped"}
    client.trc.get_torrent.side_effect = lambda *_args, **_kwargs: SimpleNamespace(status=state["status"])
    client.trc.start_torrent.side_effect = lambda **_kwargs: state.update(status="download pending")

    result = module.download(content, Path("/downloads"), "", episodes={170}, custom_words=_CUSTOM_WORDS)

    assert result[1] == torrent_hash
    assert client.add_torrent_with_status.call_args.kwargs["is_paused"] is True
    client.trc.stop_torrent.assert_not_called()
    client.trc.start_torrent.assert_called_once_with(ids=torrent_hash)


def test_transmission_stopped_after_accepted_start_never_settles_success(downloader, transmission_clock) -> None:
    """
    启动 RPC 成功而任务持续停止时，下载链必须失败结算，不能记录订阅下载成功。
    """
    module, server, content, _torrent_hash = downloader
    if not isinstance(module, TransmissionModule):
        pytest.skip("Transmission 的异步启停状态确认")
    client = _use_transmission_rpc(module, server)
    client.trc.get_torrent.return_value = SimpleNamespace(status="stopped")
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: getattr(module, method)(**kwargs)
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()

    result, message = _submit(chain, content, {170}, _CUSTOM_WORDS)

    assert result is None
    assert "启动任务未确认" in message and "未记录成功" in message
    assert transmission_clock.now == transmission_client._TORRENT_STATE_TIMEOUT
    chain._settle_accepted_download.assert_not_called()
    chain._record_rejected_download.assert_called_once()


@pytest.mark.parametrize("pause_completes", [True, False])
def test_transmission_new_task_async_pause_guards_file_selection(downloader, transmission_clock, pause_completes) -> None:
    """
    新任务异步暂停前不得选集；暂停超时也不能启动或记录订阅成功。
    """
    module, server, content, torrent_hash = downloader
    if not isinstance(module, TransmissionModule):
        pytest.skip("Transmission 的异步启停状态确认")
    client = _use_transmission_rpc(module, server)
    state = {"status": "downloading", "stop_queries": 0}
    list_files = client.get_files.side_effect

    def read_status(*_args, **_kwargs):
        """
        模拟暂停 RPC 返回后，任务仍需两次查询才能实际停止。
        """
        if client.trc.stop_torrent.called:
            state["stop_queries"] += 1
            if pause_completes and state["stop_queries"] >= 2:
                state["status"] = "stopped"
        if client.trc.start_torrent.called:
            state["status"] = "download pending"
        return SimpleNamespace(status=state["status"])

    def read_paused_files(*args, **kwargs):
        """
        文件选择只能发生在实际暂停和下一次启动之间。
        """
        assert state["status"] == "stopped"
        client.trc.start_torrent.assert_not_called()
        return list_files(*args, **kwargs)

    client.trc.get_torrent.side_effect = read_status
    client.get_files.side_effect = read_paused_files
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: getattr(module, method)(**kwargs)
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()

    result, message = _submit(chain, content, {170}, _CUSTOM_WORDS)

    client.trc.stop_torrent.assert_called_once_with(ids=torrent_hash)
    assert transmission_clock.now > 0
    if pause_completes:
        assert result == torrent_hash
        chain._settle_accepted_download.assert_called_once()
        client.trc.start_torrent.assert_called_once_with(ids=torrent_hash)
    else:
        assert result is None and "无法确认暂停" in message
        client.get_files.assert_not_called()
        client.trc.change_torrent.assert_not_called()
        client.trc.start_torrent.assert_not_called()
        chain._settle_accepted_download.assert_not_called()


def test_transmission_empty_episode_selection_never_starts_task() -> None:
    """
    选集入口没有目标集数时应保留暂停状态，不把空数组转换为全部文件。
    """
    server = Mock()
    server.stop_torrents.return_value = True
    server.ensure_torrent_state.return_value = True
    server.get_files.return_value = [SimpleNamespace(id=0, name="Show.S01E01.mkv")]

    selected, message = TransmissionModule._select_download_files(server, "hash", set(), None)

    assert selected is False
    assert "保持暂停" in message
    server.set_files.assert_not_called()
    server.set_unwanted_files.assert_not_called()
    server.start_torrents.assert_not_called()


@pytest.mark.parametrize("failure", ["unmatched", "missing_files", "priority", "start"])
def test_selection_failure_releases_new_task_and_never_settles_success(downloader, failure):
    """
    零匹配或下载器未确认选集时，清理新增任务并保留文件，不能把非空 hash 当成功。

    文件列表为空时允许一次有界启动取元数据（#7347：暂停磁力无元数据时文件列表恒为空），
    超时后先请求暂停再清理任务，不记录成功。
    """
    module, server, content, torrent_hash = downloader
    words = None if failure == "unmatched" else _CUSTOM_WORDS
    if failure == "missing_files":
        server.get_files.side_effect = None
        server.get_files.return_value = []
    elif failure == "priority":
        server.set_files.return_value = False
    elif failure == "start":
        server.start_torrents.return_value = False
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: getattr(module, method)(**kwargs)
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()

    result, message = _submit(chain, content, {170}, words)

    assert result is None
    assert torrent_hash in message
    assert ("启动任务未确认" if failure == "start" else "保持暂停") in message
    if failure == "missing_files":
        # 暂停磁力无元数据时先启动一次取元数据；超时后恢复暂停，仍不记录成功。
        server.start_torrents.assert_called_once_with(torrent_hash)
        assert server.stop_torrents.call_count == (2 if isinstance(module, TransmissionModule) else 1)
    elif failure != "start":
        server.start_torrents.assert_not_called()
    else:
        assert server.stop_torrents.call_count == (2 if isinstance(module, TransmissionModule) else 1)
        assert all(call.args == (torrent_hash,) for call in server.stop_torrents.call_args_list)
        assert "保持暂停" in message
    chain._settle_accepted_download.assert_not_called()
    chain._record_rejected_download.assert_called_once()
    server.delete_torrents.assert_called_once_with(delete_file=False, ids=torrent_hash)


@pytest.mark.parametrize("version", ["v1", "v2", "hybrid"])
@pytest.mark.parametrize("matching", [True, False])
def test_existing_task_requires_actual_info_hash(downloader, version, matching):
    """
    添加失败只可接管实际 v1/v2 身份相同的任务，同名同大小不构成证明。
    """
    module, server, _content, _torrent_hash = downloader
    content, hashes = _torrent(version)
    existing_hash = hashes[-1] if matching else "a" * 40
    server.add_torrent.return_value = (False, []) if isinstance(module, QbittorrentModule) else None
    server.add_torrent_with_status.return_value = (None, False)
    task = {"tags": ["已整理"]}
    server.get_torrents.side_effect = lambda *_args, **_kwargs: (
        [{"name": "Same.Show.S01", "total_size": 42, "hash": existing_hash, "tags": ",".join(task["tags"])}]
        if isinstance(module, QbittorrentModule) else
        [SimpleNamespace(name="Same.Show.S01", total_size=42, hashString=existing_hash, labels=list(task["tags"]))],
        None,
    )
    # 受理后会回读确认"已整理"已移除，标签写入要反映到后续查询里。
    server.remove_torrents_tag.side_effect = lambda *_args, tag=(), **_kwargs: task.update(
        tags=[item for item in task["tags"] if item not in ([tag] if isinstance(tag, str) else list(tag))],
    ) or True
    server.replace_torrent_labels.side_effect = lambda *_args, labels=(), **_kwargs: task.update(
        tags=list(labels),
    ) or True
    # 已有任务的文件已全部选中并下载完成，整包请求按原样复用结算。
    server.get_files.side_effect = lambda *_args, **_kwargs: (
        [{"id": index, "name": name, "priority": 1, "progress": 1} for index, name in enumerate(_FILES)]
        if isinstance(module, QbittorrentModule) else
        [SimpleNamespace(id=index, name=name, selected=True, completed=21, size=21) for index, name in enumerate(_FILES)]
    )

    result = module.download(content, Path("/downloads"), "", downloader="test-downloader")

    assert result[1] == (existing_hash if matching else None)
    if not matching:
        server.remove_torrents_tag.assert_not_called()
        server.set_torrents_tag.assert_not_called()
        server.set_torrent_tag.assert_not_called()


@pytest.mark.parametrize("matching", [True, False])
def test_transmission_duplicate_rpc_task_appends_target(downloader, matching):
    """
    RPC 返回重复任务时同样按目标追加；识别词不匹配时目标对不上，按失败处理且不改动任务。
    """
    module, server, content, torrent_hash = downloader
    if not isinstance(module, TransmissionModule):
        pytest.skip("Transmission RPC 重复添加返回非空 Torrent")
    _files, task = existing_task(
        module, server, torrent_hash, [f"Folder.S09E99/{name}" for name in _FILES], tags=["已整理"],
    )
    server.add_torrent_with_status.return_value = (SimpleNamespace(hashString=torrent_hash), True)

    result = module.download(
        content, Path("/downloads"), "", episodes={170},
        custom_words=_CUSTOM_WORDS if matching else None,
    )

    assert result[1] == (torrent_hash if matching else None)
    if matching:
        assert result[3] == DOWNLOAD_TASK_FILES_APPENDED
        server.set_files.assert_called_once_with(torrent_hash, [0])
        assert task["tags"] == ["MOVIEPILOT"]
    else:
        server.set_files.assert_not_called()
        server.replace_torrent_labels.assert_not_called()
    server.stop_torrents.assert_not_called()
    server.set_unwanted_files.assert_not_called()


def test_transmission_duplicate_rpc_rejects_without_changes_when_labels_cannot_be_read(downloader):
    """
    重复添加后读不到标签时，既判断不了暂停来源，也无法安全移除"已整理"：拒绝且不改动任务，
    更不能把未知标签当成空标签写回覆盖原有标签；稍后重试即可。
    """
    module, server, content, torrent_hash = downloader
    if not isinstance(module, TransmissionModule):
        pytest.skip("Transmission RPC 重复添加返回非空 Torrent")
    existing_task(module, server, torrent_hash, [f"Folder.S09E99/{name}" for name in _FILES], tags=["已整理"])
    server.add_torrent_with_status.return_value = (SimpleNamespace(hashString=torrent_hash), True)
    read_torrents = server.get_torrents.side_effect
    calls = {"count": 0}

    def fail_first_read(*args, **kwargs):
        """
        第一次（读取标签）失败，之后读取任务状态正常。
        """
        calls["count"] += 1
        return ([], True) if calls["count"] == 1 else read_torrents(*args, **kwargs)

    server.get_torrents.side_effect = fail_first_read

    result = module.download(content, Path("/downloads"), "", episodes={170}, custom_words=_CUSTOM_WORDS)

    assert result[1] is None
    assert ExistingTaskRejection.from_message(result[3]) is ExistingTaskRejection.STATE_UNAVAILABLE
    server.set_files.assert_not_called()
    server.replace_torrent_labels.assert_not_called()
    server.set_torrent_tag.assert_not_called()


@pytest.mark.parametrize("pause_result", [False, None])
def test_transmission_new_task_requires_confirmed_pause(downloader, pause_result):
    """
    新任务的暂停失败或状态未知时，不查询、重选或启动任何文件。
    """
    module, server, content, torrent_hash = downloader
    if not isinstance(module, TransmissionModule):
        pytest.skip("Transmission 新任务的暂停确认")
    server.stop_torrents.return_value = pause_result

    result = module.download(content, Path("/downloads"), "", episodes={170}, custom_words=_CUSTOM_WORDS)

    assert result[1] is None
    assert torrent_hash in result[3] and "无法确认暂停" in result[3]
    assert "保持暂停" not in result[3]
    server.stop_torrents.assert_called_once_with(torrent_hash)
    server.get_files.assert_not_called()
    server.set_files.assert_not_called()
    server.set_unwanted_files.assert_not_called()
    server.start_torrents.assert_not_called()


def test_unconfirmed_start_does_not_claim_task_is_paused(downloader):
    """
    启动与补偿暂停均未确认时，应报告未知状态并继续失败结算。
    """
    module, server, content, torrent_hash = downloader
    server.start_torrents.return_value = False
    if isinstance(module, TransmissionModule):
        server.stop_torrents.side_effect = [True, False]
    else:
        server.stop_torrents.return_value = False
    result = module.download(content, Path("/downloads"), "", episodes={170}, custom_words=_CUSTOM_WORDS)
    assert result[1] is None
    assert torrent_hash in result[3] and "启动任务未确认" in result[3] and "未记录成功" in result[3]
    assert "保持暂停" not in result[3]


@pytest.mark.parametrize("started", [True, False])
def test_qbittorrent_force_start_confirms_selection_result(downloader, started):
    """
    强制继续模式使用其实际确认结果，不能因普通启动路径未执行而误判。
    """
    module, server, content, torrent_hash = downloader
    if not isinstance(module, QbittorrentModule):
        pytest.skip("Transmission 不提供 qBittorrent 强制继续模式")
    server.is_force_resume.return_value = True
    server.torrents_set_force_start.return_value = started
    result = module.download(content, Path("/downloads"), "", episodes={170}, custom_words=_CUSTOM_WORDS)
    assert result[1] == (torrent_hash if started else None)
    server.torrents_set_force_start.assert_called_once_with(torrent_hash)
    server.start_torrents.assert_not_called()


def _report_existing_task(server, module, torrent_hash) -> None:
    """
    让真实模块把本次添加解释为下载器中已存在的同一任务。

    :param server: 被替换的外部下载器客户端
    :param module: 当前参数化的下载器模块
    :param torrent_hash: 下载器中已存在任务的哈希
    """
    server.add_torrent.return_value = (
        (False, []) if isinstance(module, QbittorrentModule) else None
    )
    server.add_torrent_with_status.return_value = (None, False)
    server.get_torrents.return_value = (
        [{"hash": torrent_hash, "name": "Same.Show.S01", "total_size": 42, "tags": "", "state": "pausedDL"}]
        if isinstance(module, QbittorrentModule) else
        [SimpleNamespace(hashString=torrent_hash, name="Same.Show.S01", total_size=42, labels=[], status="stopped")],
        None,
    )


@pytest.mark.parametrize("existing,expected", [
    (None, DownloadTaskAction.CREATED),
    ({"wanted": {0, 1}, "completed": {0, 1}}, DownloadTaskAction.REUSED),
    ({"wanted": {0}, "completed": {0}}, DownloadTaskAction.APPENDED),
    ({"wanted": {0, 1}, "stopped": True, "tags": ["MP暂停:可恢复:系统"]}, DownloadTaskAction.RESUMED),
    ({"wanted": {0, 1}, "stopped": True, "tags": ["MP暂停:暂缓:插件:NightOnly"]}, DownloadTaskAction.DEFERRED),
], ids=["created", "reused", "appended", "resumed", "deferred"])
def test_downloader_task_action_reaches_settlement(downloader, existing, expected):
    """
    新建、追加、恢复、暂缓与原样复用在结算入口必须可区分，供链路决定通知、文件明细与重复退避。
    """
    module, server, content, torrent_hash = downloader
    if existing is not None:
        existing_task(module, server, torrent_hash, [f"Folder.S09E99/{name}" for name in _FILES], **existing)
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: getattr(module, method)(**kwargs)
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()

    result, message = _submit(chain, content, None, None)

    assert result == torrent_hash
    assert chain._settle_accepted_download.call_args.kwargs["task_action"] is expected
    assert DownloadTaskAction.from_downloader_message(message) is expected
    chain._record_rejected_download.assert_not_called()


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("source", ["Subscribe|1", "MoviePilot|手动下载"])
@pytest.mark.parametrize("existing,expected_files", [
    ("matched", [_FILES[1]]),
    ("all", []),
    ("empty", _FILES),
    ("unrelated", _FILES),
], ids=["matched", "all", "empty", "unrelated"])
def test_reuse_settlement_keeps_history_and_skips_duplicate_effects(durable, source, existing, expected_files):
    """
    复用已有任务时仍记录历史并触发整理与事件，但不生成通知意图、不重复写入已记录的文件明细；
    文件明细全部已记录时才为该资源登记退避，仍有新增文件则不退避。
    """
    recorded_file, new_file = _FILES
    records = {
        "matched": [f"/downloads/shared-show/{recorded_file}", ""],
        "all": [f"/downloads/shared-show/{name}" for name in _FILES],
        "empty": [],
        "unrelated": ["/downloads/shared-show/Other.Show.S01E01.1080p.mkv"],
    }[existing]
    meta = MetaInfo(recorded_file)
    media = MediaInfo(type=MediaType.TV, title="一念永恒", tmdb_id=107371)
    torrent = TorrentInfo(title="A.Will.Eternal.S04.2160p")
    chain = DownloadChain.__new__(DownloadChain)
    chain.download_failure_repository = MagicMock()
    chain.download_history_repository = MagicMock()
    chain.download_history_repository.get_files_by_hash.return_value = [
        SimpleNamespace(fullpath=path) for path in records
    ]
    chain.durable_event_writer = MagicMock() if durable else None
    chain._build_download_notification = MagicMock(return_value=object())
    chain._submit_download_added_task = MagicMock()
    chain.post_message = MagicMock()
    chain.eventmanager = MagicMock()

    chain._settle_download_success(
        context=Context(meta_info=meta, media_info=media, torrent_info=torrent),
        media=media, meta=meta, torrent=torrent,
        folder_name="shared-show", file_list=[recorded_file, new_file],
        download_dir=Path("/downloads"), layout="Original", downloader="qb",
        download_hash="reuse-hash", download_episodes=None, episodes=None,
        channel=None, source=source, userid=None, username=None,
        torrent_content=b"torrent", custom_words=None, task_action=DownloadTaskAction.REUSED,
    )

    if durable:
        payload = chain.durable_event_writer.download_added.call_args.kwargs
        assert payload["notification_payload"] is None
        history, files = payload["history"], payload["files"]
    else:
        history, files = chain.download_history_repository.add.call_args.args
        chain.post_message.assert_not_called()
        chain._submit_download_added_task.assert_called_once()
        chain.eventmanager.send_event.assert_called_once()
    assert history.download_hash == "reuse-hash"
    assert [item.filepath for item in files] == list(expected_files)
    chain._build_download_notification.assert_not_called()
    chain.download_history_repository.get_files_by_hash.assert_called_once_with("reuse-hash", state=1)
    record_backoff = chain.download_failure_repository.record_failure
    assert record_backoff.called is (existing == "all" and source == "Subscribe|1")
    if record_backoff.called:
        write = record_backoff.call_args.args[0]
        assert write.source == source
        assert write.downloader == "qb"


def test_reused_episode_subset_keeps_other_torrent_files_available():
    """
    仅重投已记录的一集时，整包仍含未记录的另一集，不能对整个资源登记退避。
    """
    recorded_file, _new_file = _FILES
    meta = MetaInfo(recorded_file)
    media = MediaInfo(type=MediaType.TV, title="一念永恒", tmdb_id=107371)
    torrent = TorrentInfo(title="A.Will.Eternal.S04.2160p")
    chain = DownloadChain.__new__(DownloadChain)
    chain.download_history_repository = MagicMock()
    chain.download_history_repository.get_files_by_hash.return_value = [
        SimpleNamespace(fullpath=f"/downloads/shared-show/{recorded_file}"),
    ]
    chain.download_failure_repository = MagicMock()
    chain.durable_event_writer = MagicMock()

    chain._settle_download_success(
        context=Context(meta_info=meta, media_info=media, torrent_info=torrent),
        media=media, meta=meta, torrent=torrent,
        folder_name="shared-show", file_list=_FILES,
        download_dir=Path("/downloads"), layout="Original", downloader="qb",
        download_hash="reuse-hash", download_episodes=None, episodes={5},
        channel=None, source="Subscribe|1", userid=None, username=None,
        torrent_content=b"torrent", custom_words=None, task_action=DownloadTaskAction.REUSED,
    )

    chain.download_failure_repository.record_failure.assert_not_called()
    assert chain.durable_event_writer.download_added.call_args.kwargs["files"] == ()


def test_reuse_settlement_without_media_files_skips_file_lookup():
    """
    复用任务且本次种子没有可记录的媒体文件时，不再查询历史明细，也不写入文件记录。
    """
    meta = MetaInfo("readme.txt")
    media = MediaInfo(type=MediaType.TV, title="一念永恒", tmdb_id=107371)
    torrent = TorrentInfo(title="A.Will.Eternal.S04.2160p")
    chain = DownloadChain.__new__(DownloadChain)
    chain.download_history_repository = MagicMock()
    chain.durable_event_writer = MagicMock()
    chain._build_download_notification = MagicMock(return_value=object())

    chain._settle_download_success(
        context=Context(meta_info=meta, media_info=media, torrent_info=torrent),
        media=media, meta=meta, torrent=torrent,
        folder_name="shared-show", file_list=["readme.txt"],
        download_dir=Path("/downloads"), layout="Original", downloader="qb",
        download_hash="reuse-hash", download_episodes=None, episodes=None,
        channel=None, source="subscribe", userid=None, username=None,
        torrent_content=b"torrent", custom_words=None, task_action=DownloadTaskAction.REUSED,
    )

    payload = chain.durable_event_writer.download_added.call_args.kwargs
    assert payload["files"] == ()
    chain.download_history_repository.get_files_by_hash.assert_not_called()


def _settle_existing(
        task_action, *, recorded, source="Subscribe|1", selected_episodes=None, episodes=None, previous_note=None,
):
    """
    以同一整包对已有任务执行一次成功结算，返回链实例与 durable 写入参数。

    :param task_action: 下载器对已有任务的实际动作
    :param recorded: 下载历史中已记录的文件名
    :param source: 下载来源
    :param selected_episodes: 提交前冻结的订阅集数；None 表示本次没有可记录的集数
    :param episodes: 本次目标集号
    :param previous_note: 该任务最新一条下载历史的 note；None 表示没有历史
    :return: 下载链实例与 durable_event_writer.download_added 的关键字参数
    """
    meta = MetaInfo("A.Will.Eternal.S04.2160p")
    media = MediaInfo(type=MediaType.TV, title="一念永恒", tmdb_id=107371)
    torrent = TorrentInfo(title="A.Will.Eternal.S04.2160p")
    chain = DownloadChain.__new__(DownloadChain)
    chain.download_failure_repository = MagicMock()
    chain.download_history_repository = MagicMock()
    chain.download_history_repository.get_files_by_hash.return_value = [
        SimpleNamespace(fullpath=f"/downloads/shared-show/{name}") for name in recorded
    ]
    chain.download_history_repository.get_by_hash.return_value = (
        None if previous_note is None else SimpleNamespace(note=previous_note)
    )
    chain.durable_event_writer = MagicMock()
    chain._build_download_notification = MagicMock(return_value=Message(title="开始下载"))
    context = Context(meta_info=meta, media_info=media, torrent_info=torrent)
    context.selected_episodes = selected_episodes

    chain._settle_download_success(
        context=context, media=media, meta=meta, torrent=torrent,
        folder_name="shared-show", file_list=_FILES,
        download_dir=Path("/downloads"), layout="Original", downloader="qb",
        download_hash="reuse-hash", download_episodes=None, episodes=episodes,
        channel=None, source=source, userid=None, username=None,
        torrent_content=b"torrent", custom_words=None, task_action=task_action,
    )
    return chain, chain.durable_event_writer.download_added.call_args.kwargs


@pytest.mark.parametrize("task_action,previous,episodes,expected", [
    (DownloadTaskAction.CREATED, {5}, {6}, ConfirmedSelection(episodes=frozenset({6}))),
    (DownloadTaskAction.APPENDED, {5}, {6}, ConfirmedSelection(episodes=frozenset({5, 6}))),
    (DownloadTaskAction.REUSED, None, {6}, ConfirmedSelection(episodes=frozenset({6}))),
    (DownloadTaskAction.DEFERRED, {5}, None, ConfirmedSelection(episodes=frozenset({5}), whole=True)),
], ids=["created-resets", "appended-merges", "no-history", "whole-request"])
def test_settlement_accumulates_confirmed_selection_in_history_note(task_action, previous, episodes, expected):
    """
    受理后把"MP 在该任务上确认选中过的目标"写进下载历史 note：复用已有任务时与最新一条历史累积，
    新建任务从零开始（同一 hash 的旧任务可能已删除后重新添加，旧记录里的集在新任务上从未选中）。
    """
    previous_note = None if previous is None else {
        torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: ConfirmedSelection(episodes=frozenset(previous)).to_note(),
    }

    _chain, payload = _settle_existing(task_action, recorded=_FILES, episodes=episodes, previous_note=previous_note)

    assert ConfirmedSelection.from_note(payload["history"].note) == expected


def test_submission_passes_latest_confirmed_selection_and_holds_task_lock(downloader, monkeypatch):
    """
    提交前按种子 hash（大小写两种写法）读取最新一条下载历史中的确认记录交给下载器。
    读取确认记录、调用下载器、写入下载历史整段持有该 hash 的任务锁：与整理链回写、删除及暂停恢复互斥，
    两次并发追加也不会各自读到旧记录、后写的覆盖先写的而丢掉一次确认。
    """
    module, _server, content, torrent_hash = downloader
    seen = {}
    lock_states = []
    stripe = submission_module.downloader_task_locks._locks[
        submission_module.downloader_task_locks._stripe(torrent_hash)
    ]

    def fake_download(**kwargs):
        """
        记录下载器收到的确认记录与调用时的锁状态。
        """
        seen["confirmed"] = kwargs["confirmed_selection"]
        lock_states.append(("download", stripe.locked()))
        return "qb", torrent_hash, "Original", "添加下载成功"

    def read_histories(_hashes):
        """
        读取确认记录时记录锁状态。
        """
        lock_states.append(("read", stripe.locked()))
        return {
            torrent_hash.upper(): SimpleNamespace(date="2026-01-02 00:00:00", note={
                torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: ConfirmedSelection(episodes=frozenset({5, 6})).to_note(),
            }),
            torrent_hash: SimpleNamespace(date="2026-01-01 00:00:00", note={}),
        }

    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: fake_download(**kwargs)
    chain._settle_accepted_download = Mock(side_effect=lambda **_kwargs: lock_states.append(("settle", stripe.locked())))
    chain._record_rejected_download = Mock()
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_hashes.side_effect = read_histories

    _submit(chain, content, {170}, _CUSTOM_WORDS)

    queried = chain.download_history_repository.get_by_hashes.call_args.args[0]
    assert {torrent_hash, torrent_hash.upper()} <= set(queried)
    assert seen == {"confirmed": ConfirmedSelection(episodes=frozenset({5, 6}))}
    assert lock_states == [("read", True), ("download", True), ("settle", True)]
    assert not submission_module.downloader_task_locks._locks[
        submission_module.downloader_task_locks._stripe(torrent_hash)
    ].locked()


def test_submission_breaks_same_second_history_ties_by_primary_key(downloader):
    """
    大小写两种 hash 写法各查到一条、登记时间又在同一秒时，按主键取最后写入的一条，
    不能取到过期的确认记录（否则被取消的 E06 会被当成从未选中而重新选中）。
    """
    module, _server, content, torrent_hash = downloader
    seen = {}

    def fake_download(**kwargs):
        """
        记录下载器收到的确认记录。
        """
        seen["confirmed"] = kwargs["confirmed_selection"]
        return "qb", torrent_hash, "Original", "添加下载成功"

    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: fake_download(**kwargs)
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_hashes.return_value = {
        torrent_hash: SimpleNamespace(id=7, date="2026-01-01 00:00:00", note={
            torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: ConfirmedSelection(episodes=frozenset({5})).to_note(),
        }),
        torrent_hash.upper(): SimpleNamespace(id=8, date="2026-01-01 00:00:00", note={
            torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: ConfirmedSelection(episodes=frozenset({5, 6})).to_note(),
        }),
    }

    _submit(chain, content, {170}, _CUSTOM_WORDS)

    assert seen["confirmed"] == ConfirmedSelection(episodes=frozenset({5, 6}))


@pytest.mark.parametrize("histories,expected", [
    ({}, None),
    (RuntimeError("database locked"), None),
])
def test_submission_without_history_passes_no_confirmed_selection(downloader, histories, expected):
    """
    没有下载历史（外部添加的任务）或读取失败时不传确认记录：只会让被取消的文件可能被重新选中，
    不会误拒合法请求。
    """
    module, _server, content, torrent_hash = downloader
    seen = {}
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: seen.setdefault("confirmed", kwargs["confirmed_selection"]) or (
        "qb", torrent_hash, "Original", "添加下载成功"
    )
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()
    chain.download_history_repository = Mock()
    if isinstance(histories, Exception):
        chain.download_history_repository.get_by_hashes.side_effect = histories
    else:
        chain.download_history_repository.get_by_hashes.return_value = histories

    _submit(chain, content, {170}, _CUSTOM_WORDS)

    assert seen["confirmed"] is expected


@pytest.mark.parametrize("rejection,ttl", [
    (ExistingTaskRejection.PAUSED_ABANDONED, DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS),
    (ExistingTaskRejection.FILES_CANCELLED, DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS),
    (ExistingTaskRejection.EPISODES_NOT_FOUND, DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS),
    (ExistingTaskRejection.TASK_ERROR, DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS),
    (ExistingTaskRejection.PAUSED_EXTERNALLY, DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS),
    (ExistingTaskRejection.FILES_UNAVAILABLE, DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS),
    (ExistingTaskRejection.STATE_UNAVAILABLE, DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS),
    (ExistingTaskRejection.NOT_CONFIRMED, DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS),
])
def test_existing_task_rejection_cooldown_follows_reason(rejection, ttl):
    """
    已有任务的拒绝按原因冷却：资源当前用不了（放弃、取消、集数对不上、出错）按资源级冷却，
    读取失败、外部暂停、写入未确认按瞬时冷却；细节里的"删除""not found"等字样不能升级冷却。
    """
    message = rejection.describe("abc", "目标视频已被删除（not found）")

    assert DownloadFailureOwner._download_failure_ttl(message) == ttl


def test_appended_settlement_notifies_even_when_files_were_recorded():
    """
    真正追加了文件就要发新增通知，哪怕数据库早有这些文件明细（例如曾误下整季后又取消勾选）；
    文件明细仍按路径去重，也不登记重复退避。
    """
    chain, payload = _settle_existing(DownloadTaskAction.APPENDED, recorded=_FILES)

    chain._build_download_notification.assert_called_once()
    assert payload["notification_payload"] is not None
    assert payload["files"] == ()
    assert payload["event_payload"]["task_action"] == "appended"
    chain.download_failure_repository.record_failure.assert_not_called()


@pytest.mark.parametrize("task_action,selected_episodes,backoff", [
    (DownloadTaskAction.REUSED, None, True),
    (DownloadTaskAction.DEFERRED, None, True),
    (DownloadTaskAction.RESUMED, None, False),
    (DownloadTaskAction.REUSED, [5, 6], False),
    (DownloadTaskAction.APPENDED, None, False),
], ids=["reused-no-episodes", "deferred-no-episodes", "resumed-no-episodes", "reused-with-episodes", "appended"])
def test_duplicate_backoff_only_for_unrecordable_resubmission(task_action, selected_episodes, backoff):
    """
    只有下载器对任务没有任何推进（原样复用或任务被暂缓）、且本次提交记不下任何集（订阅下一轮
    必然重投）时才登记 #7107 退避。

    带集数的提交成功后订阅会记下这些集，不会循环；对它登记退避反而会挡住同一订阅之后
    扩大范围时对这个包的补集。追加文件或恢复以“可恢复”暂停的任务都算推进，不登记。
    """
    chain, payload = _settle_existing(task_action, recorded=_FILES, selected_episodes=selected_episodes)

    assert chain.download_failure_repository.record_failure.called is backoff
    assert (payload["notification_payload"] is not None) is (task_action is DownloadTaskAction.APPENDED)
