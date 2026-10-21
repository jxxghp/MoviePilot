"""
真实下载器模块与下载链的选集、身份确认边界回归。
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from torrentool.bencode import Bencode

import app.modules.transmission.transmission as transmission_client
from app.chain.download.facade import DownloadChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.modules.qbittorrent import QbittorrentModule
from app.modules.transmission import TransmissionModule
from app.modules.transmission.transmission import Transmission
from app.schemas.download import DOWNLOAD_TASK_ALREADY_EXISTS
from app.schemas.types import MediaType

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
    server.add_torrent.return_value = (
        (True, [hashes[0]]) if request.param is QbittorrentModule
        else SimpleNamespace(hashString=hashes[0])
    )
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
    client.add_torrent = server.add_torrent
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
    assert client.add_torrent.call_args.kwargs["is_paused"] is True
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
def test_transmission_async_pause_guards_file_selection(downloader, transmission_clock, pause_completes) -> None:
    """
    重复任务异步暂停前不得选集；暂停超时也不能启动或记录订阅成功。
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
def test_selection_failure_keeps_paused_task_and_never_settles_success(downloader, failure):
    """
    零匹配或下载器未确认选集时，不启动整包或把非空 hash 当成功。
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
    if failure != "start":
        server.start_torrents.assert_not_called()
    else:
        assert server.stop_torrents.call_count == (2 if isinstance(module, TransmissionModule) else 1)
        assert all(call.args == (torrent_hash,) for call in server.stop_torrents.call_args_list)
        assert "保持暂停" in message
    chain._settle_accepted_download.assert_not_called()
    chain._record_rejected_download.assert_called_once()


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
    existing = {"name": "Same.Show.S01", "total_size": 42, "hash": existing_hash, "tags": "已整理"}
    server.get_torrents.return_value = (
        [existing] if isinstance(module, QbittorrentModule) else
        [SimpleNamespace(name=existing["name"], total_size=42, hashString=existing_hash, labels=["已整理"])],
        None,
    )

    result = module.download(content, Path("/downloads"), "", downloader="test-downloader")

    assert result[1] == (existing_hash if matching else None)
    if not matching:
        server.remove_torrents_tag.assert_not_called()
        server.set_torrents_tag.assert_not_called()
        server.set_torrent_tag.assert_not_called()


def test_existing_paused_task_cannot_bypass_failed_selection(downloader):
    """
    重试遇到相同 hash 时仍须核验选集，不能把此前暂停的失败任务结算为成功。
    """
    module, server, content, torrent_hash = downloader
    server.add_torrent.return_value = (False, []) if isinstance(module, QbittorrentModule) else None
    server.get_torrents.return_value = (
        [{"hash": torrent_hash, "name": "Same.Show.S01", "total_size": 42, "tags": ""}]
        if isinstance(module, QbittorrentModule) else
        [SimpleNamespace(hashString=torrent_hash, name="Same.Show.S01", total_size=42, labels=[])],
        None,
    )
    result = module.download(content, Path("/downloads"), "", episodes={170}, downloader="test-downloader")
    assert result[1] is None
    assert "保持暂停" in result[3]
    server.start_torrents.assert_not_called()


@pytest.mark.parametrize("matching", [True, False])
def test_transmission_duplicate_rpc_task_is_paused_before_selection(downloader, matching):
    """
    RPC 把重复任务返回为非空 Torrent 时，先暂停正在运行的旧任务再选集。
    """
    module, server, content, torrent_hash = downloader
    if not isinstance(module, TransmissionModule):
        pytest.skip("Transmission RPC 重复添加返回非空 Torrent")
    task_state = {"running": True}
    list_files = server.get_files.side_effect

    def stop_task(_torrent_hash):
        """
        模拟重复添加不会修改状态、显式暂停才能停止的原任务。
        """
        task_state["running"] = False
        return True

    def get_paused_files(*args, **kwargs):
        """
        在真正读取和选择文件前验证旧任务已经暂停。
        """
        assert not task_state["running"]
        return list_files(*args, **kwargs)

    server.stop_torrents.side_effect = stop_task
    server.get_files.side_effect = get_paused_files
    result = module.download(
        content, Path("/downloads"), "", episodes={170},
        custom_words=_CUSTOM_WORDS if matching else None,
    )

    server.stop_torrents.assert_called_once_with(torrent_hash)
    assert result[1] == (torrent_hash if matching else None)
    if not matching:
        server.start_torrents.assert_not_called()
        assert not task_state["running"]
        assert "保持暂停" in result[3]


@pytest.mark.parametrize("pause_result", [False, None])
def test_transmission_duplicate_rpc_task_requires_confirmed_pause(downloader, pause_result):
    """
    重复任务的暂停失败或状态未知时，不查询、重选或启动任何文件。
    """
    module, server, content, torrent_hash = downloader
    if not isinstance(module, TransmissionModule):
        pytest.skip("Transmission RPC 重复添加返回非空 Torrent")
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
    server.get_torrents.return_value = (
        [{"hash": torrent_hash, "name": "Same.Show.S01", "total_size": 42, "tags": ""}]
        if isinstance(module, QbittorrentModule) else
        [SimpleNamespace(hashString=torrent_hash, name="Same.Show.S01", total_size=42, labels=[])],
        None,
    )


@pytest.mark.parametrize("existing", [True, False])
def test_downloader_task_identity_decides_reuse_flag(downloader, existing):
    """
    复用已有任务与新增提交在结算入口必须可区分，供链路跳过重复通知与文件明细。
    """
    module, server, content, torrent_hash = downloader
    if existing:
        _report_existing_task(server, module, torrent_hash)
    chain = DownloadChain.__new__(DownloadChain)
    chain.run_module = lambda method, **kwargs: getattr(module, method)(**kwargs)
    chain._settle_accepted_download = Mock()
    chain._record_rejected_download = Mock()

    result, message = _submit(chain, content, None, None)

    assert result == torrent_hash
    assert chain._settle_accepted_download.call_args.kwargs["already_exists"] is existing
    assert (message == DOWNLOAD_TASK_ALREADY_EXISTS) is existing
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
        torrent_content=b"torrent", custom_words=None, already_exists=True,
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
        torrent_content=b"torrent", custom_words=None, already_exists=True,
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
        torrent_content=b"torrent", custom_words=None, already_exists=True,
    )

    payload = chain.durable_event_writer.download_added.call_args.kwargs
    assert payload["files"] == ()
    chain.download_history_repository.get_files_by_hash.assert_not_called()
