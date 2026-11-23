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
from app.application.download.admission import SubscriptionDownloadGovernance
from app.application.download.failures import DownloadFailureSnapshot
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
    # 受理后会回读确认"已整理"已移除、系统种子标签已补上，标签写入要反映到后续查询里。
    server.set_torrents_tag.side_effect = lambda *_args, tags=(), **_kwargs: task.update(
        tags=task["tags"] + [item for item in tags if item not in task["tags"]],
    ) or True
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
        [
            SimpleNamespace(id=index, name=name, selected=True, completed=21, size=21)
            for index, name in enumerate(_FILES)
        ]
    )

    result = module.download(content, Path("/downloads"), "", downloader="test-downloader")

    assert result[1] == (existing_hash if matching else None)
    if not matching:
        server.remove_torrents_tag.assert_not_called()
        server.set_torrents_tag.assert_not_called()
        server.set_torrent_tag.assert_not_called()


@pytest.mark.parametrize("downloader", [TransmissionModule], indirect=True)
@pytest.mark.parametrize("matching", [True, False])
def test_transmission_duplicate_rpc_task_appends_target(downloader, matching):
    """
    RPC 返回重复任务时同样按目标追加；识别词不匹配时目标对不上，按失败处理且不改动任务。
    """
    module, server, content, torrent_hash = downloader
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


@pytest.mark.parametrize("downloader", [TransmissionModule], indirect=True)
def test_transmission_duplicate_rpc_rejects_without_changes_when_labels_cannot_be_read(downloader):
    """
    重复添加后读不到标签时，既判断不了暂停来源，也无法安全移除"已整理"：拒绝且不改动任务，
    更不能把未知标签当成空标签写回覆盖原有标签；稍后重试即可。
    """
    module, server, content, torrent_hash = downloader
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
], ids=["created", "reused", "appended"])
def test_downloader_task_action_reaches_settlement(downloader, existing, expected):
    """
    新建、追加与原样复用在结算入口必须可区分，供链路决定通知、文件明细与重复退避。
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
        task_action, *, recorded, source="Subscribe|1", selected_episodes=None, episodes=None, previous_selection=None,
        downloader_scope="", accepted_files=tuple(enumerate(_FILES)),
):
    """
    以同一整包对已有任务执行一次成功结算，返回链实例与 durable 写入参数。

    :param task_action: 下载器对已有任务的实际动作
    :param recorded: 下载历史中已记录的文件名
    :param source: 下载来源
    :param selected_episodes: 提交前冻结的订阅集数；None 表示本次没有可记录的集数
    :param episodes: 本次目标集号
    :param previous_selection: 提交前读到的确认记录；None 表示没有历史
    :param downloader_scope: 请求指向的下载器范围
    :param accepted_files: 受理后读到的下载器文件索引与路径；None 表示读取失败
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
        previous_selection=previous_selection, downloader_scope=downloader_scope,
        accepted_files=None if accepted_files is None else list(accepted_files),
    )
    return chain, chain.durable_event_writer.download_added.call_args.kwargs


def _files_on(downloader, *indexes):
    """
    构造某台下载器上确认选中过 ``_FILES`` 中指定文件的记录。
    """
    return ConfirmedSelection().record(downloader, frozenset(indexes), replace=True)


@pytest.mark.parametrize("task_action,previous,episodes,expected", [
    (DownloadTaskAction.CREATED, _files_on("qb", 0), {6}, _files_on("qb", 1)),
    (DownloadTaskAction.APPENDED, _files_on("qb", 0), {6}, _files_on("qb", 0, 1)),
    (DownloadTaskAction.REUSED, None, {6}, _files_on("qb", 1)),
    (DownloadTaskAction.REUSED, _files_on("qb", 0), None, _files_on("qb", 0, 1)),
    (DownloadTaskAction.CREATED, _files_on("tr", 0), {5}, _files_on("tr", 0).record(
        "qb", frozenset({0}), replace=True,
    )),
], ids=["created-resets", "appended-merges", "no-history", "whole-request", "other-downloader-kept"])
def test_settlement_accumulates_confirmed_selection_in_history_note(task_action, previous, episodes, expected):
    """
    受理后把"MP 在该任务上确认选中的文件"按下载器写进下载历史 note：复用已有任务时与之前的记录累积，
    新建任务替换这台下载器的旧记录（同一 hash 的旧任务可能已删除后重新添加），其他下载器的记录保留。
    文件身份取自受理后读到的下载器文件列表。
    """
    _chain, payload = _settle_existing(task_action, recorded=_FILES, episodes=episodes, previous_selection=previous)

    assert ConfirmedSelection.from_note(payload["history"].note) == expected


@pytest.mark.parametrize("task_action,expected", [
    (DownloadTaskAction.CREATED, {0, 1, 2}),
    (DownloadTaskAction.REUSED, {0}),
])
def test_new_whole_torrent_records_attachments_too(task_action, expected):
    """
    新建整包时下载器默认选中全部文件，确认记录包括字幕、字体等附件：之后用户取消的附件会被静默跳过，
    不会因为"从未记录"而被订阅重新勾选。复用已有任务的整包请求只确保视频已选中，记录也只有视频。
    """
    _chain, payload = _settle_existing(
        task_action, recorded=_FILES[:1], episodes=None,
        accepted_files=[(0, "Show.S04E05.mkv"), (1, "Show.S04E05.ass"), (2, "Fonts.7z")],
    )

    assert ConfirmedSelection.from_note(payload["history"].note).files_for("qb") == frozenset(expected)


@pytest.mark.parametrize("task_action,expected", [
    (DownloadTaskAction.CREATED, ConfirmedSelection().record("qb", frozenset(), replace=True)),
    (DownloadTaskAction.APPENDED, _files_on("qb", 0)),
])
def test_unreadable_downloader_files_record_nothing_new(task_action, expected):
    """
    受理后读不到下载器文件列表时本次不记录确认文件：新建任务清空这台下载器的旧记录，复用已有任务保留之前的记录，
    宁可之后重新选中被取消的文件，也不误拒正常请求。
    """
    _chain, payload = _settle_existing(
        task_action, recorded=_FILES, episodes={6}, previous_selection=_files_on("qb", 0), accepted_files=None,
    )

    assert ConfirmedSelection.from_note(payload["history"].note) == expected


def test_submission_passes_latest_confirmed_selection_and_holds_task_lock(downloader, monkeypatch):
    """
    提交前按种子 hash（大小写两种写法）读取最新一条下载历史中的确认记录交给下载器。
    读取确认记录、调用下载器、写入下载历史整段持有该 hash 的任务锁：与整理链回写、删除及暂停开始互斥，
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
                torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: _files_on("test-downloader", 0, 1).to_note(),
            }),
            torrent_hash: SimpleNamespace(date="2026-01-01 00:00:00", note={}),
        }

    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: fake_download(**kwargs)
    chain._settle_accepted_download = Mock(
        side_effect=lambda **_kwargs: lock_states.append(("settle", stripe.locked())),
    )
    chain._record_rejected_download = Mock()
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_hashes.side_effect = read_histories

    _submit(chain, content, {170}, _CUSTOM_WORDS)

    queried = chain.download_history_repository.get_by_hashes.call_args.args[0]
    assert {torrent_hash, torrent_hash.upper()} <= set(queried)
    assert seen == {"confirmed": _files_on("test-downloader", 0, 1)}
    assert lock_states == [("read", True), ("download", True), ("settle", True)]
    assert not submission_module.downloader_task_locks._locks[
        submission_module.downloader_task_locks._stripe(torrent_hash)
    ].locked()


@pytest.mark.parametrize("user_specified", [False, True])
def test_submission_passes_user_specified_with_the_real_record(downloader, user_specified):
    """
    "用户手动指定"作为独立参数交给下载器，由下载器决定重新选中被取消的目标、恢复暂停的任务；确认记录照常传递，
    不再用"不传记录"来表达手动指定。
    """
    _module, _server, content, torrent_hash = downloader
    seen = {}

    def fake_run_module(method, **kwargs):
        """
        记录下载器收到的两个参数。
        """
        if method == "torrent_files":
            return []
        seen.update(confirmed=kwargs["confirmed_selection"], user_specified=kwargs["user_specified"])
        return "qb", torrent_hash, "Original", "添加下载成功"

    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = fake_run_module
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_hashes.return_value = {
        torrent_hash: SimpleNamespace(id=1, date="2026-01-01 00:00:00", note={
            torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: _files_on("test-downloader", 1).to_note(),
        }),
    }
    prepared = SimpleNamespace(
        torrent_content=content, torrent=SimpleNamespace(site_cookie=""),
        download_dir=Path("/downloads"), media=SimpleNamespace(category="TV"), site_downloader=None,
    )

    chain._submit_prepared_download(
        prepared=prepared, context=Context(), episodes={170}, channel=None, source="Manual",
        downloader="test-downloader", userid=None, username=None, label=None,
        custom_words=_CUSTOM_WORDS, governance=None, user_specified=user_specified,
    )

    assert seen == {"confirmed": _files_on("test-downloader", 1), "user_specified": user_specified}


def test_subscription_cancelled_while_waiting_for_task_lock_never_submits(downloader):
    """
    等任务锁期间订阅被取消（同一任务上的整理回写、rTorrent 新建任务的标签轮询可能持锁数十秒）：取得锁后
    再检查一次，不调用下载器、不结算，也不标记"已开始产生下载副作用"。
    """
    _module, _server, content, torrent_hash = downloader
    stripe = submission_module.downloader_task_locks._locks[
        submission_module.downloader_task_locks._stripe(torrent_hash)
    ]
    checks = []

    def cancelled():
        """
        入口检查时还没取消，取得锁后的检查时已取消；同时记录检查时是否持有任务锁。
        """
        checks.append(stripe.locked())
        return len(checks) > 1

    governance = SubscriptionDownloadGovernance(cancelled=cancelled, mark_started=Mock())
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = Mock()
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_hashes.return_value = {}
    prepared = SimpleNamespace(
        torrent_content=content, torrent=SimpleNamespace(site_cookie="", title="Show"),
        download_dir=Path("/downloads"), media=SimpleNamespace(category="TV"), site_downloader=None,
    )

    result = chain._submit_prepared_download(
        prepared=prepared, context=Context(), episodes={170}, channel=None, source="Subscribe|1",
        downloader="test-downloader", userid=None, username=None, label=None,
        custom_words=_CUSTOM_WORDS, governance=governance,
    )

    assert result == (None, "订阅下载在提交前已取消")
    assert checks == [False, True]
    chain.run_module.assert_not_called()
    chain._settle_accepted_download.assert_not_called()
    chain._record_rejected_download.assert_not_called()
    governance.mark_started.assert_not_called()


def test_side_effects_are_marked_inside_the_task_lock_right_before_submission(downloader):
    """
    "开始产生下载副作用"在取得任务锁、确认未取消之后、调用下载器之前标记。
    """
    _module, _server, content, torrent_hash = downloader
    stripe = submission_module.downloader_task_locks._locks[
        submission_module.downloader_task_locks._stripe(torrent_hash)
    ]
    events = []
    governance = SubscriptionDownloadGovernance(
        cancelled=lambda: False, mark_started=lambda: events.append(("started", stripe.locked())),
    )

    def fake_run_module(method, **_kwargs):
        """
        记录下载器调用顺序；受理后读取文件列表时返回空。
        """
        if method == "torrent_files":
            return []
        events.append(("download", stripe.locked()))
        return "qb", torrent_hash, "Original", "添加下载成功"

    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = fake_run_module
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_hashes.return_value = {}
    prepared = SimpleNamespace(
        torrent_content=content, torrent=SimpleNamespace(site_cookie="", title="Show"),
        download_dir=Path("/downloads"), media=SimpleNamespace(category="TV"), site_downloader=None,
    )

    chain._submit_prepared_download(
        prepared=prepared, context=Context(), episodes={170}, channel=None, source="Subscribe|1",
        downloader="test-downloader", userid=None, username=None, label=None,
        custom_words=_CUSTOM_WORDS, governance=governance,
    )

    assert events == [("started", True), ("download", True)]


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
            torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: _files_on("test-downloader", 0).to_note(),
        }),
        torrent_hash.upper(): SimpleNamespace(id=8, date="2026-01-01 00:00:00", note={
            torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: _files_on("test-downloader", 0, 1).to_note(),
        }),
    }

    _submit(chain, content, {170}, _CUSTOM_WORDS)

    assert seen["confirmed"] == _files_on("test-downloader", 0, 1)


def test_submission_without_history_passes_no_confirmed_selection(downloader):
    """
    没有下载历史（外部添加的任务）时不传确认记录，已有任务上所有未选中的文件都按"从未选中"处理。
    """
    module, _server, content, torrent_hash = downloader
    seen = {}

    def fake_run_module(method, **kwargs):
        """
        记录下载器收到的确认记录；受理后读取文件列表时返回空。
        """
        if method == "torrent_files":
            return []
        seen["confirmed"] = kwargs["confirmed_selection"]
        return "qb", torrent_hash, "Original", "添加下载成功"

    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = fake_run_module
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_hashes.return_value = {}

    _submit(chain, content, {170}, _CUSTOM_WORDS)

    assert seen == {"confirmed": None}


def test_history_read_failure_fails_submission_without_calling_downloader(downloader):
    """
    读取下载历史失败时不能当成"没有历史"继续：受理后写回的确认记录只会剩下本次的文件，之前被取消的文件
    从此不再受保护。直接按失败处理、稍后重试，不调用下载器。
    """
    _module, _server, content, _torrent_hash = downloader
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = Mock()
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_hashes.side_effect = RuntimeError("database locked")

    result, message = _submit(chain, content, {170}, _CUSTOM_WORDS)

    assert result is None
    assert message.startswith("读取下载历史失败")
    chain.run_module.assert_not_called()
    chain._settle_accepted_download.assert_not_called()
    assert chain._record_rejected_download.call_args.kwargs["error_msg"] == message


@pytest.mark.parametrize("rejection,ttl", [
    (ExistingTaskRejection.FILES_CANCELLED, DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS),
    (ExistingTaskRejection.EPISODES_NOT_FOUND, DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS),
    (ExistingTaskRejection.TASK_ERROR, DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS),
    (ExistingTaskRejection.PAUSED, DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS),
    (ExistingTaskRejection.FILES_UNAVAILABLE, DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS),
    (ExistingTaskRejection.STATE_UNAVAILABLE, DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS),
    (ExistingTaskRejection.NOT_CONFIRMED, DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS),
])
def test_existing_task_rejection_cooldown_follows_reason(rejection, ttl):
    """
    已有任务的拒绝按原因冷却：有人处理前重试结果不变的（取消、集数对不上、出错）按资源级冷却，
    暂停、读取失败、写入未确认按瞬时冷却；细节里的"删除""not found"等字样不能升级冷却。
    """
    message = rejection.describe("abc", "目标视频已被删除（not found）")

    assert DownloadFailureOwner._download_failure_ttl(message) == ttl


class _FailureLedger:
    """
    内存中的下载失败台账，按指纹保存最后一次写入，供冷却范围测试读写。
    """

    def __init__(self) -> None:
        """
        创建空台账。
        """
        self.records: dict[str, DownloadFailureSnapshot] = {}

    def record_failure(self, failure) -> None:
        """
        保存一次失败事实。
        """
        self.records[failure.fingerprint] = DownloadFailureSnapshot(
            fingerprint=failure.fingerprint, error_message=failure.error_message, next_retry_at=failure.next_retry_at,
        )

    def get_active_by_fingerprints(self, fingerprints, now_time):
        """
        与真实仓储一致，只返回冷却结束时间晚于 ``now_time`` 的记录（两者都是同一格式的时间文本）。
        """
        return {
            item: self.records[item] for item in fingerprints
            if item in self.records and (self.records[item].next_retry_at or "") > now_time
        }

    def expire_all(self) -> None:
        """
        把全部记录的冷却结束时间改到过去，模拟记录过期。
        """
        for fingerprint, record in self.records.items():
            self.records[fingerprint] = DownloadFailureSnapshot(
                fingerprint=record.fingerprint, error_message=record.error_message, next_retry_at="2000-01-01 00:00:00",
            )


def _season_context(site_downloader=None, season=1):
    """
    构造同一个多季合集种子在某一季上的候选上下文，标题解析出该季 E01-E10。
    """
    torrent = TorrentInfo(
        title=f"Show.S{season:02d}E01-E10.1080p", site=1, site_name="站点",
        page_url="https://pt.example/details.php?id=42", site_downloader=site_downloader,
    )
    return Context(
        meta_info=MetaInfo(f"Show.S{season:02d}E01-E10.1080p"),
        media_info=MediaInfo(type=MediaType.TV, title="Show", tmdb_id=1),
        torrent_info=torrent,
    )


def _ledger_chain():
    """
    构造只带内存台账的下载链。
    """
    chain = DownloadChain.__new__(DownloadChain)
    chain.download_failure_repository = _FailureLedger()
    return chain


def _rejected(chain, context, message, *, episodes, source="Subscribe|1", downloader="qb", user_specified=False):
    """
    以真实的拒绝结算入口记录一次已有任务的拒绝。
    """
    prepared = SimpleNamespace(
        media=context.media_info, torrent=context.torrent_info, meta=context.meta_info, site_downloader=None,
    )
    chain._record_rejected_download(
        prepared=prepared, context=context, episodes=episodes, channel=None, source=source,
        downloader=downloader, userid=None, actual_downloader=None, error_msg=message, user_specified=user_specified,
    )


def test_existing_task_rejections_never_block_candidates():
    """
    已有任务的拒绝不让订阅跳过候选：用户恢复暂停的任务、在下载器里重新勾选文件或删掉旧任务后，
    下一轮就要交给下载器按实时状态重新判断，不能被旧记录挡住。
    """
    chain = _ledger_chain()
    chain.post_message = Mock()
    context = _season_context(site_downloader="qb")

    for rejection in (ExistingTaskRejection.PAUSED, ExistingTaskRejection.FILES_CANCELLED):
        _rejected(chain, context, rejection.describe("abc", "原因"), episodes={3})

    assert len(chain.download_failure_repository.records) == 2
    assert chain._active_download_failure_fingerprints([context], "Subscribe|1") == {}
    assert chain._active_download_failure_fingerprints([context], "Subscribe|1", downloader="qb") == {}


def test_repeated_rejection_is_notified_once_per_reason_and_scope():
    """
    同一原因、同一范围在记录有效期内重复出现时只记日志，不再通知、不顺延记录；原因变了、换了目标集、
    换了季（多季合集里 S01E03 与 S02E03 是不同目标）或换了下载器都照常通知。用户手动指定种子时每次都通知。
    """
    chain = _ledger_chain()
    chain.post_message = Mock()
    context = _season_context()
    cancelled = ExistingTaskRejection.FILES_CANCELLED.describe("abc", "E03 已被取消")

    _rejected(chain, context, cancelled, episodes={3})
    _rejected(chain, context, cancelled, episodes={3})
    assert chain.post_message.call_count == 1
    assert len(chain.download_failure_repository.records) == 1

    _rejected(chain, context, cancelled, episodes={4})
    _rejected(chain, _season_context(season=2), cancelled, episodes={3})
    _rejected(chain, context, cancelled, episodes={3}, downloader="tr")
    _rejected(chain, context, ExistingTaskRejection.PAUSED.describe("abc", "任务已暂停"), episodes={3})
    _rejected(chain, context, cancelled, episodes={3}, source="Manual", user_specified=True)
    assert chain.post_message.call_count == 6


def test_rejection_is_notified_again_after_record_expires():
    """
    静默只在记录有效期内生效：记录过期后同一原因、同一范围再次被拒时重新通知，并写入新的记录。
    """
    chain = _ledger_chain()
    chain.post_message = Mock()
    context = _season_context()
    paused = ExistingTaskRejection.PAUSED.describe("abc", "任务已暂停")

    _rejected(chain, context, paused, episodes={3})
    _rejected(chain, context, paused, episodes={3})
    assert chain.post_message.call_count == 1

    chain.download_failure_repository.expire_all()
    _rejected(chain, context, paused, episodes={3})
    _rejected(chain, context, paused, episodes={3})

    assert chain.post_message.call_count == 2
    (record,) = chain.download_failure_repository.records.values()
    assert record.next_retry_at > "2000-01-01 00:00:00"


@pytest.mark.parametrize("source", [None, "Workflow|7", "插件名"])
def test_other_automated_sources_are_deduplicated_too(source):
    """
    工作流、插件等非订阅的自动流程也会定时重复提交（工作流失败时不写缓存，下一次照样提交）：同一原因、同一范围
    在静默期内只通知一次，与订阅共用同一份记录。
    """
    chain = _ledger_chain()
    chain.post_message = Mock()
    context = _season_context()
    paused = ExistingTaskRejection.PAUSED.describe("abc", "任务已暂停")

    for _ in range(3):
        _rejected(chain, context, paused, episodes={3}, source=source)
    _rejected(chain, context, paused, episodes={3}, source="Subscribe|1")

    assert chain.post_message.call_count == 1


def test_record_with_another_reason_does_not_silence_a_new_reason():
    """
    同一任务上次因为暂停被拒，这次变成出错：原因不同，照常通知并按新原因记录。
    """
    chain = _ledger_chain()
    chain.post_message = Mock()
    context = _season_context()

    _rejected(chain, context, ExistingTaskRejection.PAUSED.describe("abc", "任务已暂停"), episodes={3})
    _rejected(chain, context, ExistingTaskRejection.TASK_ERROR.describe("abc", "文件缺失"), episodes={3})

    assert chain.post_message.call_count == 2
    (record,) = chain.download_failure_repository.records.values()
    assert ExistingTaskRejection.from_message(record.error_message) is ExistingTaskRejection.TASK_ERROR


def test_duplicate_backoff_is_scoped_to_the_requested_downloader():
    """
    #7107 重复提交退避按"订阅 + 下载器范围"登记：订阅改用另一台下载器后，同一资源会在那台下载器上新建任务，
    不能被这条退避挡住。
    """
    chain, _payload = _settle_existing(DownloadTaskAction.REUSED, recorded=_FILES, downloader_scope="qb")

    (failure,), _kwargs = chain.download_failure_repository.record_failure.call_args
    context = Context(meta_info=MetaInfo("A.Will.Eternal.S04.2160p"),
                      media_info=MediaInfo(type=MediaType.TV, title="一念永恒", tmdb_id=107371),
                      torrent_info=TorrentInfo(title="A.Will.Eternal.S04.2160p"))
    assert failure.fingerprint == DownloadChain._build_download_failure_fingerprint(
        context, resource_backoff=True, source="Subscribe|1", downloader_scope="qb",
    )
    assert failure.fingerprint != DownloadChain._build_download_failure_fingerprint(
        context, resource_backoff=True, source="Subscribe|1", downloader_scope="tr",
    )


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
    (DownloadTaskAction.REUSED, [5, 6], False),
    (DownloadTaskAction.APPENDED, None, False),
    (DownloadTaskAction.RESUMED, None, False),
], ids=["reused-no-episodes", "reused-with-episodes", "appended", "resumed"])
def test_duplicate_backoff_only_for_unrecordable_resubmission(task_action, selected_episodes, backoff):
    """
    只有下载器对任务没有任何推进（原样复用）、且本次提交记不下任何集（订阅下一轮必然重投）时才登记
    #7107 退避。

    带集数的提交成功后订阅会记下这些集，不会循环；对它登记退避反而会挡住同一订阅之后
    扩大范围时对这个包的补集。追加文件、恢复暂停的任务都算推进，不登记，并照常发送下载通知。
    """
    chain, payload = _settle_existing(task_action, recorded=_FILES, selected_episodes=selected_episodes)

    assert chain.download_failure_repository.record_failure.called is backoff
    notified = task_action in (DownloadTaskAction.APPENDED, DownloadTaskAction.RESUMED)
    assert (payload["notification_payload"] is not None) is notified


def test_rejection_ledger_skips_contexts_without_resource_or_subscription():
    """
    非订阅来源没有订阅范围；没有种子信息的候选生成不了指纹，既不参与冷却查询，也不判定为"已通知过"，照常通知。
    """
    chain = _ledger_chain()
    bare = Context(meta_info=MetaInfo("Show.S01E01-E10.1080p"), media_info=MediaInfo(type=MediaType.TV, title="Show"))

    assert DownloadChain._subscription_scope("Manual") == ""
    assert DownloadChain._subscription_scope(None) == ""
    assert chain._active_download_failure_fingerprints([bare], "Subscribe|1") == {}
    assert chain._existing_task_rejection_noted(
        bare, rejection=ExistingTaskRejection.PAUSED, downloader_scope="qb", episodes={3},
    ) is False


def test_rejection_ledger_read_failure_counts_as_not_noted():
    """
    读取已有任务拒绝记录失败时按"没通知过"处理，照常通知，宁可多通知一次也不吞掉拒绝原因。
    """
    chain = _ledger_chain()
    chain.download_failure_repository.get_active_by_fingerprints = Mock(side_effect=RuntimeError("db down"))

    assert chain._existing_task_rejection_noted(
        _season_context(), rejection=ExistingTaskRejection.PAUSED, downloader_scope="qb", episodes={3},
    ) is False


@pytest.mark.parametrize(
    ("downloader_arg", "site_downloader", "user_specified", "expected_fragments"),
    [
        ("stale-qb", None, False, ("未找到可用下载器实例", "stale-qb")),
        (None, "site-qb", False, ("未找到站点", "站点", "site-qb")),
        (None, "manual-qb", True, ("未找到可用下载器实例", "manual-qb")),
        (None, None, False, ("未找到可用的默认下载器实例",)),
    ],
    ids=["explicit-downloader", "site-downloader", "manual-site-downloader", "default-downloader"],
)
def test_missing_downloader_reports_target_without_fallback_or_cooldown(
    downloader_arg,
    site_downloader,
    user_specified,
    expected_fragments,
):
    """
    下载器模块均未匹配到可用实例时，必须明确指明请求的目标与来源，不静默回退默认下载器，
    也不将正常种子写入资源级失败冷却台账。
    """
    content, _hashes = _torrent()
    context = _season_context(site_downloader=site_downloader)
    prepared = SimpleNamespace(
        torrent_content=content,
        torrent=context.torrent_info,
        media=context.media_info,
        meta=context.meta_info,
        download_dir=Path("/downloads"),
        download_episodes=None,
        folder_name="",
        file_list=[],
        site_downloader=site_downloader,
    )
    chain = _ledger_chain()
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_hashes.return_value = {}
    chain.download = Mock(return_value=None)
    chain.post_message = Mock()

    download_hash, error_msg = chain._submit_prepared_download(
        prepared=prepared,
        context=context,
        episodes=None,
        channel=None,
        source="Manual" if user_specified else "Subscribe|1",
        downloader=downloader_arg,
        userid=None,
        username=None,
        label=None,
        custom_words=None,
        governance=None,
        user_specified=user_specified,
    )

    assert download_hash is None
    assert chain.download.call_count == 1
    assert chain.download.call_args.kwargs["downloader"] == (downloader_arg or site_downloader)
    for fragment in expected_fragments:
        assert fragment in error_msg
    assert chain.post_message.call_count == 1
    assert error_msg in chain.post_message.call_args.args[0].text
    assert chain.download_failure_repository.records == {}


def test_downloader_rejection_tuple_still_records_failure_cooldown():
    """
    下载器明确返回失败四元组时，应原样透出原因并照常写入资源失败冷却台账。
    """
    content, _hashes = _torrent()
    context = _season_context()
    prepared = SimpleNamespace(
        torrent_content=content,
        torrent=context.torrent_info,
        media=context.media_info,
        meta=context.meta_info,
        download_dir=Path("/downloads"),
        download_episodes=None,
        folder_name="",
        file_list=[],
        site_downloader=None,
    )
    chain = _ledger_chain()
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_hashes.return_value = {}
    chain.download = Mock(return_value=(None, None, None, "添加种子任务失败：demo.torrent"))
    chain.post_message = Mock()

    download_hash, error_msg = chain._submit_prepared_download(
        prepared=prepared,
        context=context,
        episodes=None,
        channel=None,
        source="Subscribe|1",
        downloader="qb",
        userid=None,
        username=None,
        label=None,
        custom_words=None,
        governance=None,
    )

    assert download_hash is None
    assert error_msg == "添加种子任务失败：demo.torrent"
    assert len(chain.download_failure_repository.records) == 1
    assert chain.post_message.call_count == 1


@pytest.mark.parametrize(
    ("downloader_arg", "issues", "expected_fragments"),
    [
        (
            "qb-main",
            [
                {
                    "id": "QbittorrentModule",
                    "name": "Qbittorrent",
                    "type": "downloader",
                    "lifecycle": "degraded",
                    "error": "qb-main：连接被拒绝",
                    "init_errors": {"qb-main": "连接被拒绝"},
                }
            ],
            ("未找到可用下载器实例", "qb-main", "实例初始化失败：连接被拒绝"),
        ),
        (
            None,
            [
                {
                    "id": "QbittorrentModule",
                    "name": "Qbittorrent",
                    "type": "downloader",
                    "lifecycle": "failed",
                    "error": "密码错误",
                    "init_errors": {},
                }
            ],
            ("未找到可用的默认下载器实例", "下载器模块未就绪：Qbittorrent（密码错误）"),
        ),
        (
            None,
            [
                {
                    "id": "QbittorrentModule",
                    "name": "Qbittorrent",
                    "type": "downloader",
                    "lifecycle": "degraded",
                    "error": "qb-default：端口无效",
                    "init_errors": {"qb-default": "端口无效"},
                }
            ],
            ("未找到可用的默认下载器实例", "部分下载器实例初始化失败：qb-default：端口无效"),
        ),
    ],
    ids=["target-instance-init-error", "module-failed", "default-degraded"],
)
def test_missing_downloader_appends_runtime_failure_diagnostics(
    downloader_arg,
    issues,
    expected_fragments,
):
    """
    下载器模块初始化或热重载失败导致实例缺失时，错误提示必须附带本地运行态故障线索且仍不写资源冷却。
    """
    content, _hashes = _torrent()
    context = _season_context()
    prepared = SimpleNamespace(
        torrent_content=content,
        torrent=context.torrent_info,
        media=context.media_info,
        meta=context.meta_info,
        download_dir=Path("/downloads"),
        download_episodes=None,
        folder_name="",
        file_list=[],
        site_downloader=None,
    )
    chain = _ledger_chain()
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_hashes.return_value = {}
    chain.download = Mock(return_value=None)
    chain.post_message = Mock()
    chain.modulemanager = Mock()
    chain.modulemanager.get_unhealthy_module_issues.return_value = issues

    download_hash, error_msg = chain._submit_prepared_download(
        prepared=prepared,
        context=context,
        episodes=None,
        channel=None,
        source="Subscribe|1",
        downloader=downloader_arg,
        userid=None,
        username=None,
        label=None,
        custom_words=None,
        governance=None,
    )

    assert download_hash is None
    for fragment in expected_fragments:
        assert fragment in error_msg
    assert chain.download_failure_repository.records == {}
    chain.modulemanager.get_unhealthy_module_issues.assert_called_once()
