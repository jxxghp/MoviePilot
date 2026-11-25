"""
下载器“已受理但任务尚未确认”状态的防重、持久化核对与自动结算回归测试（Issue #7551）。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from torrentool.bencode import Bencode

import app.chain.download.batch as download_batch
import app.chain.download.pending as download_pending_module
import app.chain.download.selection as download_selection
from app.application.download.admission import SubscriptionDownloadGovernance
from app.application.download.pending import (
    DownloadPendingWrite,
    pending_blocked_tv_scope,
    pending_blocks_movie_music,
    pending_blocks_tv_seasons,
    pending_targets_for_scan,
)
from app.application.subscription.contract import SubscriptionSnapshot
from app.chain.download.facade import DownloadChain
from app.chain.subscribe.facade import SubscribeChain
from app.chain.transfer.facade import TransferChain
from app.db.adapters.download import (
    TransactionalDownloadFailureRepository,
    TransactionalDownloadPendingRepository,
)
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.downloadpending import DownloadPending
from app.db.session import SessionFactory
from app.domain.context import Context, MediaInfo, MusicInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.modules.qbittorrent import QbittorrentModule
from app.modules.rtorrent import RtorrentModule
from app.modules.transmission import TransmissionModule
from app.schemas.download import (
    DOWNLOAD_TASK_ALREADY_EXISTS,
    PendingDownloadCheckResult,
    PendingDownloadCheckStatus,
    is_unconfirmed_download_message,
)
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType


def _build_torrent_bytes(name: str, files: list[str] | None = None) -> tuple[bytes, str]:
    """
    构造内存合法 v1 种子字节及其小写 SHA1 哈希。
    """
    info: dict[str, object] = {"name": name, "piece length": 16384, "pieces": b"x" * 20}
    if files:
        info["files"] = [{"length": 1024, "path": [item]} for item in files]
    else:
        info["length"] = 1024
    raw = Bencode.encode({"announce": b"https://tracker.invalid/announce?passkey=secret", "info": info})
    return raw, hashlib.sha1(Bencode.encode(info)).hexdigest()


def _movie_context(title: str, *, media_id: str = "27205", site: int = 1, pri_order: int = 90) -> Context:
    """
    构造电影候选上下文，用于批量下载与跨轮次防重验证。
    """
    meta = MetaInfo(title)
    meta.type = MediaType.MOVIE
    return Context(
        meta_info=meta,
        media_info=MediaInfo(
            title="Inception",
            year="2010",
            type=MediaType.MOVIE,
            media_source=MediaSource.TMDB,
            media_id=media_id,
            category="电影",
        ),
        torrent_info=TorrentInfo(
            site=site,
            site_name="TestSite",
            title=title,
            enclosure=f"https://tracker.invalid/download.php?id={title}&passkey=secret",
            site_cookie="uid=1; pass=secret",
            site_ua="SecretUA/1.0",
            size=10 * 1024 * 1024 * 1024,
            pri_order=pri_order,
        ),
    )


def _tv_context(
    title: str,
    *,
    media_id: str = "1399",
    season: int = 1,
    episodes: tuple[int, ...] = (),
) -> Context:
    """
    构造电视剧候选上下文。
    """
    meta = MetaInfo(f"{title} S{season:02d}")
    meta.type = MediaType.TV
    if episodes:
        meta.set_episode_list(list(episodes))
    return Context(
        meta_info=meta,
        media_info=MediaInfo(
            title="TestShow",
            year="2024",
            type=MediaType.TV,
            media_source=MediaSource.TMDB,
            media_id=media_id,
            category="电视剧",
        ),
        torrent_info=TorrentInfo(site=1, site_name="TestSite", title=title, pri_order=90),
    )


def _build_downloader_module(module_cls, server):
    """
    构造隔离外部配置的真实下载器模块实例。
    """
    module = module_cls.__new__(module_cls)
    module.get_instance = MagicMock(return_value=server)
    module.normalize_path = MagicMock(side_effect=lambda path, _downloader: str(path))
    module.get_default_config_name = MagicMock(return_value="qb")
    return module


@pytest.fixture(autouse=True)
def _clean_pending_state():
    """
    隔离每个用例前后的数据库待确认记录和进程内降级缓存。
    """
    download_pending_module._clear_fallback_pending_records()
    with SessionFactory() as session:
        session.query(DownloadPending).delete()
        session.commit()
    yield
    download_pending_module._clear_fallback_pending_records()
    with SessionFactory() as session:
        session.query(DownloadPending).delete()
        session.commit()


def test_qbittorrent_download_locates_newly_added_task_by_expected_hash_after_tag_timeout(monkeypatch):
    """
    qB 临时标签轮询超时后，按已知哈希查到带本次随机标签的任务时应正常确认并清理标签。
    """
    content, expected_hash = _build_torrent_bytes("Inception.2010.mkv")
    monkeypatch.setattr("app.modules.qbittorrent.text_tools.random_string", lambda _n: "tmp-tag-99")
    server = MagicMock()
    server.add_torrent.return_value = (True, [])
    server.get_torrent_id_by_tag.return_value = None
    server.get_torrents.return_value = ([{"hash": expected_hash, "tags": "MOVIEPILOT, tmp-tag-99"}], False)
    server.delete_torrents_tag.return_value = True
    server.get_content_layout.return_value = "Original"
    server.is_force_resume.return_value = False
    module = _build_downloader_module(QbittorrentModule, server)

    result = module.download(content=content, download_dir=Path("/downloads"), cookie="", downloader="qb")

    assert result == ("qb", expected_hash, "Original", "添加下载成功")
    server.delete_torrents_tag.assert_called_once_with(expected_hash, "tmp-tag-99")


def test_qbittorrent_download_recognizes_silently_deduplicated_existing_task_after_tag_timeout(monkeypatch):
    """
    qB 对重复种子返回 Ok. 但不打本次随机标签时，按哈希兜底识别为已有任务并走已有任务核验。
    """
    content, expected_hash = _build_torrent_bytes("Inception.2010.mkv")
    monkeypatch.setattr("app.modules.qbittorrent.text_tools.random_string", lambda _n: "tmp-tag-88")
    server = MagicMock()
    server.add_torrent.return_value = (True, [])
    server.get_torrent_id_by_tag.return_value = None
    server.get_torrents.side_effect = lambda *_args, **_kwargs: (
        [{"hash": expected_hash, "tags": "MOVIEPILOT", "state": "uploading"}],
        False,
    )
    server.get_files.side_effect = lambda *_args, **_kwargs: [
        {"id": 0, "name": "Inception.2010.mkv", "priority": 1, "progress": 1, "size": 1024},
    ]
    server.get_content_layout.return_value = "Original"
    module = _build_downloader_module(QbittorrentModule, server)

    result = module.download(content=content, download_dir=Path("/downloads"), cookie="", downloader="qb")

    assert result == ("qb", expected_hash, "Original", DOWNLOAD_TASK_ALREADY_EXISTS)
    server.delete_torrents_tag.assert_called_once_with(None, "tmp-tag-88")


@pytest.mark.parametrize("module_cls", [QbittorrentModule, RtorrentModule, TransmissionModule])
def test_confirm_pending_download_distinguishes_query_error_not_found_and_confirmed(module_cls):
    """
    三个内置下载器核对待确认哈希时必须严格区分查询出错、暂未出现和已确认落地。
    """
    _content, expected_hash = _build_torrent_bytes("Inception.2010.mkv")
    server = MagicMock()
    server.get_content_layout.return_value = "Original"
    server.is_force_resume.return_value = False
    module = _build_downloader_module(module_cls, server)

    # 1. 下载器查询报错 -> QUERY_ERROR
    server.get_torrents.return_value = ([], True)
    error_res = module.confirm_pending_download(hashes=[expected_hash], downloader="qb")
    assert error_res is not None
    assert error_res.status is PendingDownloadCheckStatus.QUERY_ERROR

    # 2. 下载器在线但尚未出现 -> NOT_FOUND
    server.get_torrents.return_value = ([], False)
    missing_res = module.confirm_pending_download(hashes=[expected_hash], downloader="qb")
    assert missing_res is not None
    assert missing_res.status is PendingDownloadCheckStatus.NOT_FOUND

    # 3. 任务延迟出现在下载器 -> CONFIRMED
    if module_cls is QbittorrentModule:
        server.get_torrents.side_effect = lambda *_a, **_kw: (
            [{"hash": expected_hash, "state": "downloading", "tags": "MOVIEPILOT"}],
            False,
        )
    elif module_cls is RtorrentModule:
        server.get_torrents.side_effect = lambda *_a, **_kw: (
            [{"hash": expected_hash.upper(), "state": 1, "is_active": 1, "tags": "MOVIEPILOT"}],
            False,
        )
    else:
        server.get_torrents.side_effect = lambda *_a, **_kw: (
            [SimpleNamespace(hashString=expected_hash, status="downloading", error=0, labels=["MOVIEPILOT"])],
            False,
        )
    confirmed_res = module.confirm_pending_download(hashes=[expected_hash], downloader="qb")
    assert confirmed_res is not None
    assert confirmed_res.status is PendingDownloadCheckStatus.CONFIRMED
    assert confirmed_res.download_hash.lower() == expected_hash
    assert confirmed_res.downloader == "qb"


def test_confirm_pending_download_selects_episodes_when_paused_tv_task_appears():
    """
    带 episodes 以暂停状态提交的 TV 任务延迟出现在 qB 后，核对时应自动补做选集并启动。
    """
    _content, expected_hash = _build_torrent_bytes(
        "Show.S01",
        files=["Show.S01E01.mkv", "Show.S01E02.mkv"],
    )
    server = MagicMock()
    server.get_torrents.side_effect = lambda *_a, **_kw: (
        [{"hash": expected_hash, "state": "pausedDL", "tags": "MOVIEPILOT"}],
        False,
    )
    server.get_files.side_effect = lambda *_a, **_kw: [
        {"id": 0, "name": "Show.S01/Show.S01E01.mkv", "priority": 1, "progress": 0, "size": 1024},
        {"id": 1, "name": "Show.S01/Show.S01E02.mkv", "priority": 1, "progress": 0, "size": 1024},
    ]
    server.set_files.return_value = True
    server.start_torrents.return_value = True
    server.get_content_layout.return_value = "Original"
    server.is_force_resume.return_value = False
    module = _build_downloader_module(QbittorrentModule, server)

    result = module.confirm_pending_download(
        hashes=[expected_hash],
        downloader="qb",
        episodes={1},
    )

    assert result is not None
    assert result.status is PendingDownloadCheckStatus.CONFIRMED
    assert result.download_hash == expected_hash
    assert server.set_files.call_args_list[0].kwargs == {
        "torrent_hash": expected_hash,
        "file_ids": [1],
        "priority": 0,
    }
    assert server.set_files.call_args_list[1].kwargs == {
        "torrent_hash": expected_hash,
        "file_ids": [0],
        "priority": 1,
    }
    server.start_torrents.assert_called_once_with(expected_hash)


def test_movie_batch_stops_submitting_other_versions_after_unconfirmed_acceptance(monkeypatch):
    """
    复现 Issue #7551：电影候选第 1 个版本已受理但确认超时，绝不得继续提交其余 16 个不同版本。
    """
    chain = object.__new__(DownloadChain)
    chain.eventmanager = SimpleNamespace(send_event=MagicMock(return_value=None))
    chain._active_download_failure_fingerprints = MagicMock(return_value={})
    chain._record_download_failure = MagicMock()
    chain._log_download_failure_cooldown = MagicMock()
    chain.download_single = MagicMock(
        return_value=(None, "下载任务添加成功，但获取Qbittorrent任务信息失败：Release-A.mkv")
    )
    helper = SimpleNamespace(sort_torrents=MagicMock(side_effect=lambda items: items))
    stop = SimpleNamespace(is_system_stopped=False)
    monkeypatch.setattr(download_batch, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_selection, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_batch, "runtime_stop_state", stop)
    monkeypatch.setattr(download_selection, "runtime_stop_state", stop)

    candidates = [_movie_context(f"Release-{chr(65 + idx)}") for idx in range(17)]
    downloaded, _lefts = chain.batch_download(contexts=candidates, source='Subscribe|{"id": 42}')

    assert downloaded == []
    # 只提交了第 1 个候选 Release-A，其余 16 个不同版本全部被阻止。
    assert chain.download_single.call_count == 1
    assert chain.download_single.call_args.args[0] is candidates[0]


def test_movie_batch_distinguishes_explicit_rejection_from_unconfirmed_acceptance(monkeypatch):
    """
    明确拒绝添加的候选允许继续换下一个版本；一旦出现已受理但未确认，立即停止继续换源。
    """
    chain = object.__new__(DownloadChain)
    chain.eventmanager = SimpleNamespace(send_event=MagicMock(return_value=None))
    chain._active_download_failure_fingerprints = MagicMock(return_value={})
    chain._record_download_failure = MagicMock()
    chain._log_download_failure_cooldown = MagicMock()
    chain.download_single = MagicMock(
        side_effect=[
            (None, "下载种子出错，状态码：404"),
            (None, "下载任务添加成功，但获取Qbittorrent任务信息失败：Release-B.mkv"),
            ("should-not-run", "添加下载成功"),
        ]
    )
    helper = SimpleNamespace(sort_torrents=MagicMock(side_effect=lambda items: items))
    stop = SimpleNamespace(is_system_stopped=False)
    monkeypatch.setattr(download_batch, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_selection, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_batch, "runtime_stop_state", stop)
    monkeypatch.setattr(download_selection, "runtime_stop_state", stop)

    candidates = [_movie_context("Release-A"), _movie_context("Release-B"), _movie_context("Release-C")]
    downloaded, _lefts = chain.batch_download(contexts=candidates)

    assert downloaded == []
    assert chain.download_single.call_count == 2
    assert chain.download_single.call_args_list[0].args[0] is candidates[0]
    assert chain.download_single.call_args_list[1].args[0] is candidates[1]


def test_tv_batch_blocks_same_season_and_episodes_on_unconfirmed_without_blocking_other_targets(monkeypatch):
    """
    电视剧某季或某集出现已受理未确认时，只拦截重叠季集的其他版本，不误伤其他季或不重叠的集。
    """
    chain = object.__new__(DownloadChain)
    chain.eventmanager = SimpleNamespace(send_event=MagicMock(return_value=None))
    chain._active_download_failure_fingerprints = MagicMock(return_value={})
    chain._record_download_failure = MagicMock()
    chain._log_download_failure_cooldown = MagicMock()
    chain.download_torrent = MagicMock(return_value=(b"torrent", None, [1, 2, 3]))
    helper = SimpleNamespace(
        sort_torrents=MagicMock(side_effect=lambda items: items),
        get_torrent_episodes=MagicMock(side_effect=lambda files, **_kw: files),
    )
    stop = SimpleNamespace(is_system_stopped=False)
    monkeypatch.setattr(download_batch, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_selection, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_batch, "runtime_stop_state", stop)
    monkeypatch.setattr(download_selection, "runtime_stop_state", stop)

    s1_pack_a = _tv_context("Show.S01.A", season=1)
    s1_pack_b = _tv_context("Show.S01.B", season=1)
    s1_ep1 = _tv_context("Show.S01E01", season=1, episodes=(1,))
    s2_pack = _tv_context("Show.S02.A", season=2)
    chain.download_single = MagicMock(
        side_effect=[
            (None, "下载任务添加成功，但获取Qbittorrent任务信息失败：Show.S01.A"),
            ("s2-hash", "添加下载成功"),
        ]
    )
    missing = {
        "tmdb:1399": {
            1: NotExistMediaInfo(season=1, episodes=[], total_episode=3, start_episode=1),
            2: NotExistMediaInfo(season=2, episodes=[], total_episode=3, start_episode=1),
        }
    }

    downloaded, remaining = chain.batch_download([s1_pack_a, s1_pack_b, s1_ep1, s2_pack], missing)

    assert downloaded == [s2_pack]
    assert chain.download_single.call_count == 2
    assert set(remaining["tmdb:1399"]) == {1}


def test_unconfirmed_submission_persists_sanitized_pending_and_reconciles_to_history_and_subscription():
    """
    端到端闭环：qB 超时写入脱敏待确认记录 -> 下一轮拦住替代版本 -> 延迟落地后核对补记历史并完成订阅。
    """
    pending_repo = TransactionalDownloadPendingRepository(SessionFactory)
    failure_repo = TransactionalDownloadFailureRepository(SessionFactory)
    content_a, hash_a = _build_torrent_bytes("Inception.2010.Release-A.mkv")
    ctx_a = _movie_context("Release-A")
    source_str = 'Subscribe|{"id": 42, "name": "Inception", "type": "电影"}'
    governance_reports: list[tuple[str, str | None]] = []
    governance = SubscriptionDownloadGovernance(
        decision_observer=lambda _ctx, decision, reason: governance_reports.append((decision, reason))
    )

    chain = DownloadChain()
    chain.download_pending_repository = pending_repo
    chain.download_failure_repository = failure_repo
    chain.post_message = MagicMock()
    chain.download = MagicMock(
        return_value=(None, None, None, "下载任务添加成功，但获取Qbittorrent任务信息失败：Release-A.mkv")
    )

    prepared = SimpleNamespace(
        torrent=ctx_a.torrent_info,
        media=ctx_a.media_info,
        meta=ctx_a.meta_info,
        torrent_content=content_a,
        folder_name="",
        file_list=["Inception.2010.Release-A.mkv"],
        download_dir=Path("/downloads/movies"),
        download_uri="/downloads/movies",
        download_episodes=None,
        site_downloader="qb",
    )
    download_hash, error_msg = chain._submit_prepared_download(
        prepared=prepared,
        context=ctx_a,
        episodes=None,
        channel=None,
        source=source_str,
        downloader="qb",
        userid="u1",
        username="alice",
        label=None,
        custom_words=None,
        governance=governance,
    )
    assert download_hash is None
    assert is_unconfirmed_download_message(error_msg)

    # 1. 验证已持久化待确认记录，且载荷不含敏感凭据与下载地址。
    stored = pending_repo.list_all()
    assert len(stored) == 1
    record = stored[0]
    assert record.hashes == (hash_a,)
    assert record.subscription_id == 42
    assert record.downloader == "qb"
    assert record.payload is not None
    serialized_text = str(record.payload)
    assert "passkey=secret" not in serialized_text
    assert "pass=secret" not in serialized_text
    assert "SecretUA" not in serialized_text
    # 不应写入普通失败冷却台账。
    fp = chain._build_download_failure_fingerprint(ctx_a)
    assert fp is not None
    assert failure_repo.get_active_by_fingerprints([fp], time.strftime("%Y-%m-%d %H:%M:%S")) == {}

    # 2. 下一轮核对时 qB 尚未加载出该任务（NOT_FOUND 且未超期）-> 保持待确认，阻止 Release-B 提交。
    chain.confirm_pending_download = MagicMock(
        return_value=PendingDownloadCheckResult(
            status=PendingDownloadCheckStatus.NOT_FOUND,
            downloader="qb",
        )
    )
    chain.download_single = MagicMock(return_value=("should-not-submit", "添加下载成功"))
    ctx_b = _movie_context("Release-B")
    downloaded_round2, _ = chain.batch_download(contexts=[ctx_b], source=source_str, downloader="qb")
    assert downloaded_round2 == []
    chain.download_single.assert_not_called()
    assert len(pending_repo.list_all()) == 1

    # 3. 44 分钟后任务终于出现在 qB -> 核对确认，自动补记 DownloadHistory、删除待确认并收敛订阅！
    chain.confirm_pending_download = MagicMock(
        return_value=PendingDownloadCheckResult(
            status=PendingDownloadCheckStatus.CONFIRMED,
            downloader="qb",
            download_hash=hash_a,
            layout="Original",
            message="添加下载成功",
        )
    )
    chain._resolve_actual_download_dir = MagicMock(return_value=Path("/downloads/movies"))
    chain._accepted_task_files = MagicMock(return_value=[(0, "Inception.2010.Release-A.mkv")])
    settled_calls: list[dict[str, object]] = []
    chain._settle_download_success = MagicMock(side_effect=lambda **kw: settled_calls.append(kw))

    reconciled = chain.reconcile_pending_downloads(subscription_id=42)
    assert len(reconciled.confirmed) == 1
    assert reconciled.still_pending == ()
    assert pending_repo.list_all() == []
    assert len(settled_calls) == 1
    assert settled_calls[0]["download_hash"] == hash_a
    assert settled_calls[0]["downloader"] == "qb"

    # 验证 SubscribeChain 接到回执后能够完成对应电影订阅。
    subscribe = SubscriptionSnapshot(
        id=42,
        name="Inception",
        year="2010",
        type=MediaType.MOVIE.value,
        media_source=MediaSource.TMDB,
        media_id="27205",
        best_version=0,
        state="R",
    )
    sub_chain = object.__new__(SubscribeChain)
    sub_chain.subscription_repository = SimpleNamespace(get=MagicMock(return_value=subscribe))
    sub_chain.finish_subscribe_or_not = MagicMock()

    settled_count = sub_chain.settle_confirmed_pending_downloads(reconciled.confirmed)
    assert settled_count == 1
    sub_chain.finish_subscribe_or_not.assert_called_once()
    finish_kwargs = sub_chain.finish_subscribe_or_not.call_args.kwargs
    assert finish_kwargs["subscribe"] is subscribe
    assert len(finish_kwargs["downloads"]) == 1
    assert finish_kwargs["downloads"][0].torrent_info.title == "Release-A"


def test_pending_not_released_on_query_error_even_after_expiry_but_released_on_confirmed_absence():
    """
    超过保护期后：下载器查询出错绝不释放待确认；只有下载器在线确认查无此任务时才安全释放。
    """
    pending_repo = TransactionalDownloadPendingRepository(SessionFactory)
    write = DownloadPendingWrite(
        pending_key="expired-pending-1",
        target_key="电影:themoviedb:27205",
        submitted_at="2026-11-22 20:00:00",
        expires_at="2026-11-22 22:00:00",
        media_type=MediaType.MOVIE.value,
        title="Inception",
        year="2010",
        media_source=MediaSource.TMDB,
        media_id="27205",
        downloader="qb",
        hashes=("a" * 40,),
        torrent_name="Release-A",
        source='Subscribe|{"id": 42}',
        subscription_id=42,
    )
    pending_repo.upsert(write)

    chain = DownloadChain()
    chain.download_pending_repository = pending_repo
    chain.download_history_repository = SimpleNamespace(get_by_hashes=MagicMock(return_value={}))

    # 1. 下载器离线 / 查询出错：即使已过期也必须继续保留待确认保护。
    chain.confirm_pending_download = MagicMock(
        return_value=PendingDownloadCheckResult(
            status=PendingDownloadCheckStatus.QUERY_ERROR,
            downloader="qb",
            message="无法连接Qbittorrent下载器",
        )
    )
    res_err = chain.reconcile_pending_downloads()
    assert len(res_err.still_pending) == 1
    assert res_err.released == ()
    assert len(pending_repo.list_all()) == 1

    # 2. 下载器恢复在线且明确返回 NOT_FOUND，且已超过保护期 -> 安全释放。
    chain.confirm_pending_download = MagicMock(
        return_value=PendingDownloadCheckResult(
            status=PendingDownloadCheckStatus.NOT_FOUND,
            downloader="qb",
        )
    )
    res_released = chain.reconcile_pending_downloads()
    assert res_released.still_pending == ()
    assert len(res_released.released) == 1
    assert pending_repo.list_all() == []


def test_pending_conflict_and_scan_target_helpers():
    """
    纯领域冲突与分页搜索暂停目标计算覆盖电影、整季与离散集数边界。
    """
    movie_pending = DownloadPendingWrite(
        pending_key="k1",
        target_key="电影:themoviedb:1",
        submitted_at="2026-11-22 21:43:28",
        expires_at="2026-11-22 23:43:28",
        media_type=MediaType.MOVIE.value,
    )
    movie_snap = download_pending_module._snapshot_from_write(movie_pending)
    assert pending_blocks_movie_music([movie_snap], "电影:themoviedb:1") is movie_snap
    assert pending_targets_for_scan([movie_snap], "电影:themoviedb:1", MediaType.MOVIE) == {"movie"}

    tv_ep_snap = download_pending_module._snapshot_from_write(
        DownloadPendingWrite(
            pending_key="k2",
            target_key="电视剧:themoviedb:2",
            submitted_at="2026-11-22 21:43:28",
            expires_at="2026-11-22 23:43:28",
            media_type=MediaType.TV.value,
            season=1,
            episodes=(1, 2),
        )
    )
    whole_blocked, blocked_eps = pending_blocked_tv_scope([tv_ep_snap], "电视剧:themoviedb:2", 1)
    assert whole_blocked is False
    assert blocked_eps == frozenset({1, 2})
    assert pending_blocks_tv_seasons([tv_ep_snap], "电视剧:themoviedb:2", [1]) is True
    assert pending_blocks_tv_seasons([tv_ep_snap], "电视剧:themoviedb:2", [2]) is False
    missing_seasons = {1: NotExistMediaInfo(season=1, episodes=[1, 2, 3], total_episode=3, start_episode=1)}
    assert pending_targets_for_scan(
        [tv_ep_snap], "电视剧:themoviedb:2", MediaType.TV, missing_seasons
    ) == {"1:1", "1:2"}


def test_full_pack_first_does_not_fallback_to_episodes_when_full_pack_is_pending(monkeypatch):
    """
    全集优先尝试阶段出现已受理未确认时，订阅策略不得回退到按集下载。
    """
    pending_repo = TransactionalDownloadPendingRepository(SessionFactory)
    tv_ctx = _tv_context("Show.S01.FullPack", media_id="1399", season=1)
    pending_repo.upsert(
        DownloadPendingWrite(
            pending_key="tv-full-pending",
            target_key="电视剧:tmdb:1399",
            submitted_at="2026-11-22 21:43:28",
            expires_at="2099-11-22 23:43:28",
            media_type=MediaType.TV.value,
            season=1,
            episodes=None,
            hashes=("b" * 40,),
            torrent_name="Show.S01.FullPack",
        )
    )
    subscribe = SubscriptionSnapshot(
        id=9,
        name="TestShow",
        year="2024",
        type=MediaType.TV.value,
        season=1,
        total_episode=3,
        media_source=MediaSource.TMDB,
        media_id="1399",
        best_version=0,
        state="R",
    )
    ep_ctx = _tv_context("Show.S01E01", media_id="1399", season=1, episodes=(1,))
    no_exists = {
        "tmdb:1399": {1: NotExistMediaInfo(season=1, episodes=[], total_episode=3, start_episode=1)}
    }

    batch_calls: list[list[Context]] = []

    class _StubDownloadChain(DownloadChain):
        """
        保留真实的待确认查询，同时拦截批量下载调用以记录回退次数。
        """

        def __init__(self) -> None:
            """
            绑定测试待确认仓储。
            """
            self.download_pending_repository = pending_repo

        def batch_download(self, **kwargs):
            """
            模拟首次全集提交进入待确认并返回空下载列表。
            """
            batch_calls.append(kwargs["contexts"])
            return [], kwargs["no_exists"]

    import app.chain.subscribe.policy as subscribe_policy

    monkeypatch.setattr(subscribe_policy, "DownloadChain", _StubDownloadChain)
    sub_chain = object.__new__(SubscribeChain)
    sub_chain.subscription_repository = None

    downloads, lefts = sub_chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[tv_ctx, ep_ctx],
        no_exists=no_exists,
        subscribe=subscribe,
        mediakey="tmdb:1399",
    )

    assert downloads == []
    assert lefts == no_exists
    # 只调用了一次全集尝试，没有回退到第二次按集下载。
    assert len(batch_calls) == 1
    assert batch_calls[0] == [tv_ctx]


def test_transfer_chain_reconciles_pending_before_scanning_completed_torrents():
    """
    下载器整理扫描前先触发待确认核对，确保延迟落地任务在整理前补齐历史与订阅状态。
    """
    events: list[str] = []
    chain = object.__new__(TransferChain)
    chain.download_pending_repository = object()
    chain._transfer_subscribe_chain = lambda: SimpleNamespace(
        reconcile_pending_downloads=lambda: events.append("reconcile")
    )
    chain.list_torrents = MagicMock(side_effect=lambda **_kw: events.append("list_torrents") or [])
    chain.jobview = SimpleNamespace(get_all_torrent_hashes=MagicMock(return_value=set()))

    import app.chain.transfer.queue as transfer_queue

    original_helper = transfer_queue.DirectoryHelper
    transfer_queue.DirectoryHelper = lambda: SimpleNamespace(
        get_download_dirs=lambda: [SimpleNamespace(monitor_type="downloader")]
    )
    try:
        chain.process()
    finally:
        transfer_queue.DirectoryHelper = original_helper

    assert events == ["reconcile", "list_torrents"]


@pytest.mark.parametrize(
    ("pending_downloader", "histories", "already_settled"),
    [
        pytest.param("qb", [("qb", "22:27:05")], True, id="same-instance"),
        pytest.param("qb", [("qb", "22:27:05"), ("qb-other", "22:28:05")], True, id="same-instance-shadowed"),
        pytest.param("qb", [("qb-other", "22:28:05")], False, id="other-instance"),
        pytest.param("qb", [("qb", "21:42:00"), ("qb-other", "22:28:05")], False, id="old-same-instance"),
        pytest.param("", [("", "22:28:05")], False, id="unknown-instance"),
    ],
)
def test_reconcile_pending_recovers_only_from_current_instance_history(
    db, pending_downloader, histories, already_settled,
):
    """
    同一下载器在提交后写入的历史可用于幂等恢复。其他实例或归属不明的历史仍须向下载器核对。
    """
    db.watermark(DownloadPending)
    pending_repo = TransactionalDownloadPendingRepository(SessionFactory)
    target_hash = "c" * 40
    db.add(*[
        DownloadHistory(
            path="/downloads/Inception",
            type=MediaType.MOVIE.value,
            title="Inception",
            downloader=history_downloader,
            download_hash=target_hash,
            date=f"2026-11-22 {history_time}",
        )
        for history_downloader, history_time in histories
    ])
    pending_repo.upsert(
        DownloadPendingWrite(
            pending_key="idempotent-pending",
            target_key="电影:tmdb:27205",
            submitted_at="2026-11-22 21:43:28",
            expires_at="2099-11-22 23:43:28",
            media_type=MediaType.MOVIE.value,
            title="Inception",
            year="2010",
            media_source=MediaSource.TMDB,
            media_id="27205",
            downloader=pending_downloader,
            hashes=(target_hash,),
            torrent_name="Release-A",
            source='Subscribe|{"id": 42}',
            subscription_id=42,
        )
    )
    chain = DownloadChain()
    chain.download_pending_repository = pending_repo
    chain.confirm_pending_download = MagicMock(
        return_value=PendingDownloadCheckResult(
            status=PendingDownloadCheckStatus.NOT_FOUND,
            downloader=pending_downloader or None,
        )
    )
    chain._settle_download_success = MagicMock()

    result = chain.reconcile_pending_downloads(subscription_id=42)

    chain._settle_download_success.assert_not_called()
    if already_settled:
        assert [(item.downloader, item.download_hash) for item in result.confirmed] == [(pending_downloader, target_hash)]
        assert pending_repo.get_by_key("idempotent-pending") is None
        chain.confirm_pending_download.assert_not_called()
    else:
        assert result.confirmed == ()
        assert [item.pending_key for item in result.still_pending] == ["idempotent-pending"]
        assert pending_repo.get_by_key("idempotent-pending") is not None
        chain.confirm_pending_download.assert_called_once()
        assert chain.confirm_pending_download.call_args.kwargs["downloader"] == (pending_downloader or None)


def test_music_batch_blocks_same_music_after_unconfirmed_without_blocking_other_music(monkeypatch):
    """
    音乐批量下载遇到已受理未确认时，只停止同音乐目标的后续版本，不影响同批另一音乐作品。
    """
    chain = object.__new__(DownloadChain)
    chain.eventmanager = SimpleNamespace(send_event=MagicMock(return_value=None))
    chain._active_download_failure_fingerprints = MagicMock(return_value={})
    chain._record_download_failure = MagicMock()
    chain._log_download_failure_cooldown = MagicMock()
    chain.download_single = MagicMock(
        side_effect=[
            (None, "下载任务添加成功，但获取rTorrent任务信息失败：Album-A-FLAC"),
            ("album-b-hash", "添加下载成功"),
        ]
    )
    helper = SimpleNamespace(sort_torrents=MagicMock(side_effect=lambda items: items))
    stop = SimpleNamespace(is_system_stopped=False)
    monkeypatch.setattr(download_batch, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_selection, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_batch, "runtime_stop_state", stop)
    monkeypatch.setattr(download_selection, "runtime_stop_state", stop)

    def _music_ctx(title: str, media_id: str) -> Context:
        meta = MetaInfo(title)
        meta.type = MediaType.MUSIC
        return Context(
            meta_info=meta,
            media_info=MusicInfo(
                title=f"Album-{media_id}",
                year=2024,
                media_source=MediaSource.MusicBrainz,
                media_id=media_id,
            ),
            torrent_info=TorrentInfo(site=1, site_name="MusicSite", title=title),
        )

    album_a1 = _music_ctx("Album-A-FLAC", "mb-1")
    album_a2 = _music_ctx("Album-A-MP3", "mb-1")
    album_b = _music_ctx("Album-B-FLAC", "mb-2")

    downloaded, _ = chain.batch_download([album_a1, album_a2, album_b])

    assert downloaded == [album_b]
    assert chain.download_single.call_count == 2
    assert chain.download_single.call_args_list[0].args[0] is album_a1
    assert chain.download_single.call_args_list[1].args[0] is album_b


def test_paged_subscription_search_stops_paging_after_movie_becomes_pending(monkeypatch):
    """
    分页电影搜索第 1 页提交后进入待确认状态，应立即暂停 movie 目标不再翻深页，且不误完成订阅。
    """
    import app.chain.subscribe.search as subscribe_search

    pending_repo = TransactionalDownloadPendingRepository(SessionFactory)
    subscribe = SubscriptionSnapshot(
        id=77,
        name="Inception",
        year="2010",
        type=MediaType.MOVIE.value,
        media_source=MediaSource.TMDB,
        media_id="27205",
        best_version=0,
        state="R",
    )
    movie = MediaInfo(
        title="Inception",
        year="2010",
        type=MediaType.MOVIE,
        media_source=MediaSource.TMDB,
        media_id="27205",
    )
    meta = MetaInfo("Inception 2010")
    meta.type = MediaType.MOVIE
    target = SimpleNamespace(subscribe=subscribe, meta=meta, media=movie, media_key="tmdb:27205", missing={})

    class _StubDownloadChain(DownloadChain):
        """
        使用真实待确认仓储，便于验证分页搜索回调读到刚写入的待确认状态。
        """

        def __init__(self) -> None:
            """
            绑定待确认持久化仓储。
            """
            self.download_pending_repository = pending_repo

    captured_settled: list[set[str]] = []

    class _FakeScan:
        """
        模拟触发第一页提交并记录返回的 settled 目标。
        """

        def __init__(self, **_kwargs) -> None:
            """
            保留与 SearchScan 兼容的初始化入口。
            """

        def run(self, submit) -> None:
            """
            用第一页电影候选触发 submit 并记录返回集合。
            """
            captured_settled.append(submit([_movie_context("Release-A")], {"movie"}))

    def _simulate_unconfirmed_batch(**_kwargs):
        """
        模拟第一页提交后 qB 受理但未确认：写入待确认记录且返回空 downloads。
        """
        pending_repo.upsert(
            DownloadPendingWrite(
                pending_key="paged-movie-pending",
                target_key="电影:tmdb:27205",
                submitted_at="2026-11-22 21:43:28",
                expires_at="2099-11-22 23:43:28",
                media_type=MediaType.MOVIE.value,
                title="Inception",
                year="2010",
                media_source=MediaSource.TMDB,
                media_id="27205",
                downloader="qb",
                hashes=("d" * 40,),
                torrent_name="Release-A",
                source='Subscribe|{"id": 77}',
                subscription_id=77,
            )
        )
        return [], {}

    owner = SimpleNamespace(
        subscription_repository=SimpleNamespace(get=MagicMock(return_value=subscribe)),
        get_sub_sites=MagicMock(return_value=[1]),
        get_params=MagicMock(return_value={}),
        get_subscribe_source_keyword=MagicMock(return_value='Subscribe|{"id": 77}'),
        _filter_search_contexts=MagicMock(side_effect=lambda _sub, items: items),
        _SubscribeChain__build_full_pack_first_no_exists=MagicMock(return_value=None),
        _SubscribeChain__download_best_version_with_full_pack_first=MagicMock(side_effect=_simulate_unconfirmed_batch),
        _SubscribeChain__candidate_contract_changed=MagicMock(return_value=False),
        resolve_subscribe_missing=MagicMock(return_value=(False, {})),
        finish_subscribe_or_not=MagicMock(),
        reconcile_pending_downloads=MagicMock(return_value=download_pending_module.PendingReconciliationResult()),
    )

    monkeypatch.setattr(subscribe_search, "DownloadChain", _StubDownloadChain)
    monkeypatch.setattr(subscribe_search, "SearchScan", _FakeScan)
    monkeypatch.setattr(subscribe_search, "_subscription_search_strategy", lambda _best: "smart")

    from app.schemas.types import SystemConfigKey

    subscribe_search._process_paged_subscription(
        owner,
        subscribe,
        SimpleNamespace(),
        target,
        SystemConfigKey.SubscribeFilterRuleGroups,
        None,
    )

    # 第一页进入待确认后，返回 {"movie"} 让 SearchScan 立即停止继续翻深页。
    assert captured_settled == [{"movie"}]
    # 收尾检查传入的是 downloads=[], force=False，不会把未确认的电影订阅误判为完成。
    owner.finish_subscribe_or_not.assert_called_once()
    assert owner.finish_subscribe_or_not.call_args.kwargs["downloads"] == []
    assert owner.finish_subscribe_or_not.call_args.kwargs["force"] is False


def test_pending_repository_crud_and_rollback_boundaries():
    """
    待确认仓储适配器的查询、更新、按目标删除与异常回滚合同必须保持完整。
    """
    repo = TransactionalDownloadPendingRepository(SessionFactory)
    write = DownloadPendingWrite(
        pending_key="repo-crud-key",
        target_key="电视剧:tmdb:999",
        submitted_at="2026-11-22 21:43:28",
        expires_at="2099-11-22 23:43:28",
        media_type=MediaType.TV.value,
        title="Show",
        year="2026",
        media_source=MediaSource.TMDB,
        media_id="999",
        season=1,
        episodes=(1, 2),
        downloader="qb",
        hashes=("e" * 40,),
        torrent_name="Show.S01E01-E02",
        source='Subscribe|{"id": 91}',
        subscription_id=91,
        payload={
            "nested": {"list": [1, 2]},
            "episodes": (1, 2),
        },
    )
    repo.upsert(write)
    loaded = repo.get_by_key("repo-crud-key")
    assert loaded is not None
    assert loaded.episodes == (1, 2)
    assert loaded.payload is not None
    assert loaded.payload["nested"]["list"] == (1, 2)
    assert len(repo.list_by_target_keys(["电视剧:tmdb:999"])) == 1
    assert len(repo.list_by_subscription_id(91)) == 1
    assert repo.list_by_target_keys([]) == []

    repo.touch_checked("repo-crud-key", "2026-11-22 21:50:00")
    assert repo.get_by_key("repo-crud-key").last_checked_at == "2026-11-22 21:50:00"
    assert repo.delete_by_target_key("电视剧:tmdb:999") == 1
    assert repo.get_by_key("repo-crud-key") is None

    # 验证写事务出错时回滚并向上传播异常。
    failing_session = MagicMock()
    failing_session.__enter__.return_value = failing_session
    failing_session.__exit__.return_value = False
    failing_repo = TransactionalDownloadPendingRepository(lambda: failing_session)
    with pytest.raises(RuntimeError, match="boom"):
        failing_repo._execute_write(lambda _s: (_ for _ in ()).throw(RuntimeError("boom")))
    failing_session.rollback.assert_called_once()


def test_pending_fallback_cache_and_third_party_list_torrents_reconcile():
    """
    DB 写入异常时进程内兜底缓存维持拦截，第三方下载器未实现 confirm_pending_download 时降级 list_torrents。
    """
    from app.domain import torrent as torrent_rules
    from app.schemas.types import NotificationChannel

    # 验证序列化/反序列化与脱敏还原覆盖音乐、季集映射、通知渠道等字段。
    selection = torrent_rules.EpisodeSelection(season=2, source_season=1, episode_map=((1, 13), (2, 14)))
    serialized = download_pending_module._serialize_episode_selection(selection)
    restored = download_pending_module._deserialize_episode_selection(serialized)
    assert restored == selection
    assert download_pending_module._deserialize_episode_selection({"season": True}) is None
    assert download_pending_module._restore_channel(NotificationChannel.Telegram.value) is NotificationChannel.Telegram
    assert download_pending_module._restore_channel("invalid-channel") is None
    assert download_pending_module._parse_subscription_id("Subscribe|{bad") is None
    assert download_pending_module._build_pending_target_key(None) == ""

    music_media = MusicInfo(
        title="Random Access Memories",
        year=2013,
        media_source=MediaSource.MusicBrainz,
        media_id="rg-42",
        music_type="album",
        category="电子",
        cover_url="https://img.example/cover.jpg",
    )
    music_record = download_pending_module._snapshot_from_write(
        DownloadPendingWrite(
            pending_key="music-pending-key",
            target_key=download_pending_module._build_pending_target_key(music_media),
            submitted_at="2026-11-22 21:43:28",
            expires_at="2099-11-22 23:43:28",
            media_type=MediaType.MUSIC.value,
            title="Random Access Memories",
            year="2013",
            media_source=MediaSource.MusicBrainz,
            media_id="rg-42",
            downloader="custom-dl",
            hashes=("f" * 40,),
            torrent_name="Daft.Punk.FLAC",
            source='Subscribe|{"id": 55}',
            subscription_id=55,
            payload={
                "media": {
                    "type": MediaType.MUSIC.value,
                    "title": "Random Access Memories",
                    "year": "2013",
                    "media_source": MediaSource.MusicBrainz.value,
                    "media_id": "rg-42",
                    "music_type": "album",
                    "poster_path": "https://img.example/cover.jpg",
                },
                "meta": {"org_string": "Daft.Punk.FLAC", "season_list": [1], "episode_list": [1, 2]},
                "torrent": {"title": "Daft.Punk.FLAC", "site": 1, "site_name": "PT", "size": 100},
                "subscription_download": {"priority": 1},
                "music_track_keys": ["01-track"],
                "confirmed_full_coverage": True,
                "episode_selection": serialized,
                "channel": NotificationChannel.Telegram.value,
            },
        )
    )

    failing_repo = MagicMock()
    failing_repo.upsert.side_effect = RuntimeError("database is locked")
    failing_repo.list_all.side_effect = RuntimeError("database is locked")
    failing_repo.get_by_key.side_effect = RuntimeError("database is locked")
    failing_repo.delete_by_key.side_effect = RuntimeError("database is locked")
    failing_repo.delete_by_target_key.side_effect = RuntimeError("database is locked")
    failing_repo.touch_checked.side_effect = RuntimeError("database is locked")

    chain = object.__new__(DownloadChain)
    chain.download_pending_repository = failing_repo
    chain.download_history_repository = None
    chain._existing_task_confirmed_selection = MagicMock(return_value=None)
    chain._resolve_actual_download_dir = MagicMock(return_value=Path("/downloads"))
    chain._accepted_task_files = MagicMock(return_value=())
    chain._settle_download_success = MagicMock()
    chain.confirm_pending_download = MagicMock(return_value=None)

    # DB 锁死时自动退回进程内兜底缓存，list_pending_downloads 仍可检出并还原音乐 Context。
    write_obj = DownloadPendingWrite(
        pending_key=music_record.pending_key,
        target_key=music_record.target_key,
        submitted_at=music_record.submitted_at,
        expires_at=music_record.expires_at,
        media_type=music_record.media_type,
        title=music_record.title,
        year=music_record.year,
        media_source=music_record.media_source,
        media_id=music_record.media_id,
        downloader=music_record.downloader,
        hashes=music_record.hashes,
        torrent_name=music_record.torrent_name,
        source=music_record.source,
        subscription_id=music_record.subscription_id,
        payload=music_record.payload,
    )
    chain._persist_pending_write(write_obj, music_record)
    assert len(chain.list_pending_downloads(subscription_id=55)) == 1
    assert chain.list_pending_downloads(target_keys=["电影:tmdb:1"]) == []

    # 第三方下载器 list_torrents 返回 None 或抛错 -> QUERY_ERROR 保守保留。
    chain.list_torrents = MagicMock(return_value=None)
    assert len(chain.reconcile_pending_downloads().still_pending) == 1
    chain.list_torrents = MagicMock(side_effect=RuntimeError("offline"))
    assert len(chain.reconcile_pending_downloads().still_pending) == 1
    chain.list_torrents = MagicMock(return_value=[])
    assert len(chain.reconcile_pending_downloads().still_pending) == 1

    # 第三方下载器列表出现对应哈希 -> CONFIRMED 并转正结算、清理兜底缓存。
    chain.list_torrents = MagicMock(return_value=[SimpleNamespace(hash="f" * 40, downloader="custom-dl")])
    settled = chain.reconcile_pending_downloads()
    assert len(settled.confirmed) == 1
    assert settled.confirmed[0].context.music_track_keys == ["01-track"]
    assert settled.confirmed[0].context.subscription_download == {"priority": 1}
    chain._settle_download_success.assert_called_once()
    assert chain.list_pending_downloads() == []

    # clear_pending_downloads_for_media 清理兜底缓存，REJECTED 核对结果立即释放。
    chain._persist_pending_write(write_obj, music_record)
    assert chain.clear_pending_downloads_for_media(music_media) == 1
    assert chain.clear_pending_downloads_for_media(None) == 0

    chain._persist_pending_write(write_obj, music_record)
    chain.confirm_pending_download = MagicMock(
        return_value=PendingDownloadCheckResult(
            status=PendingDownloadCheckStatus.REJECTED,
            downloader="custom-dl",
            message="任务处于错误状态",
        )
    )
    rejected_result = chain.reconcile_pending_downloads()
    assert len(rejected_result.released) == 1
    assert chain.list_pending_downloads() == []


def test_tv_complete_and_partial_pack_unconfirmed_blocks_only_affected_episodes():
    """
    电视剧完整集与多集拆包阶段遇到未确认提交时，仅封锁受影响集号并放行其余缺失集。
    """
    repo = TransactionalDownloadPendingRepository(SessionFactory)
    media = MediaInfo(
        title="Andor",
        year="2022",
        type=MediaType.TV,
        media_source=MediaSource.TMDB,
        media_id="83867",
    )
    meta_e1 = MetaInfo("Andor.S01E01.1080p")
    meta_e1.type = MediaType.TV
    meta_e1.begin_season = 1
    meta_e1.set_episode_list([1])
    ctx_e1_a = Context(meta_info=meta_e1, media_info=media, torrent_info=TorrentInfo(title="Andor.S01E01.A", size=100))

    meta_e1_b = MetaInfo("Andor.S01E01.2160p")
    meta_e1_b.type = MediaType.TV
    meta_e1_b.begin_season = 1
    meta_e1_b.set_episode_list([1])
    ctx_e1_b = Context(meta_info=meta_e1_b, media_info=media, torrent_info=TorrentInfo(title="Andor.S01E01.B", size=200))

    meta_pack = MetaInfo("Andor.S01E02-E03.1080p")
    meta_pack.type = MediaType.TV
    meta_pack.begin_season = 1
    meta_pack.set_episode_list([2, 3])
    ctx_pack_a = Context(meta_info=meta_pack, media_info=media, torrent_info=TorrentInfo(title="Andor.S01E02-E03.A", size=300))

    meta_pack_b = MetaInfo("Andor.S01E02-E03.2160p")
    meta_pack_b.type = MediaType.TV
    meta_pack_b.begin_season = 1
    meta_pack_b.set_episode_list([2, 3])
    ctx_pack_b = Context(meta_info=meta_pack_b, media_info=media, torrent_info=TorrentInfo(title="Andor.S01E02-E03.B", size=400))

    chain = object.__new__(DownloadChain)
    chain.download_pending_repository = repo
    chain.download_failure_repository = TransactionalDownloadFailureRepository(SessionFactory)
    chain.eventmanager = SimpleNamespace(send_event=MagicMock(return_value=None))
    chain._load_active_download_failures = MagicMock(return_value={})
    submitted: list[str] = []

    def _download_single(context: Context, *, return_detail: bool = False, **kwargs):
        submitted.append(context.torrent_info.title)
        msg = "下载任务添加成功，但获取Qbittorrent任务信息失败：temp-tag"
        return (None, msg) if return_detail else None

    chain.download_single = _download_single
    pack_bytes, _ = _build_torrent_bytes("Andor.Pack", files=["Andor.S01E02.mkv", "Andor.S01E04.mkv"])
    chain._fetch_torrent_content = MagicMock(return_value=pack_bytes)

    no_exists = {
        "tmdb:83867": {
            1: NotExistMediaInfo(season=1, episodes=[1, 2], total_episode=4, start_episode=1),
        }
    }
    options = download_batch._BatchDownloadOptions(
        save_path=None,
        channel=None,
        source=None,
        userid=None,
        username=None,
        downloader="qb",
        custom_words=None,
        governance=None,
    )
    run = download_batch._BatchDownloadRun(
        chain,
        [ctx_e1_a, ctx_e1_b, ctx_pack_a, ctx_pack_b],
        no_exists,
        options,
        None,
    )
    run._read_torrent = MagicMock(return_value=(pack_bytes, [2, 4]))
    run.run()
    assert run.downloaded == []
    # E01 的 A 版进入待确认后拦住了 B 版；多集拆包选中 E02 进入待确认后也拦住了拆包 B 版。
    assert submitted == ["Andor.S01E01.A", "Andor.S01E02-E03.A"]
    assert ("tmdb:83867", 1, 1) in run.unconfirmed_episodes
    assert ("tmdb:83867", 1, 2) in run.unconfirmed_episodes
    assert run.no_exists["tmdb:83867"][1].episodes == [1, 2]


def test_batch_reconciles_confirmed_and_pending_tv_across_same_and_other_sources():
    """
    批量下载开始前核对同源/异源刚确认与仍待确认的电视剧季集，精确扣减或封锁对应范围。
    """
    from app.application.download.pending import ConfirmedPendingDownload, PendingReconciliationResult

    tv_media_a = MediaInfo(
        title="ShowA", year="2024", type=MediaType.TV, media_source=MediaSource.TMDB, media_id="1001"
    )
    tv_media_b = MediaInfo(
        title="ShowB", year="2024", type=MediaType.TV, media_source=MediaSource.TMDB, media_id="1002"
    )

    def _tv_snap(
        key: str,
        target_key: str,
        *,
        season: int,
        episodes: tuple[int, ...] | None,
        source: str,
    ):
        return download_pending_module._snapshot_from_write(
            DownloadPendingWrite(
                pending_key=key,
                target_key=target_key,
                submitted_at="2026-11-22 21:43:28",
                expires_at="2099-11-22 23:43:28",
                media_type=MediaType.TV.value,
                title="Show",
                year="2024",
                media_source=MediaSource.TMDB,
                media_id=target_key.split(":")[-1],
                season=season,
                episodes=episodes,
                downloader="qb",
                hashes=("a" * 40,),
                torrent_name=f"Show-{key}",
                source=source,
            )
        )

    confirmed = (
        # 同源整季刚确认 -> 扣减 S01 并计入 downloaded
        ConfirmedPendingDownload(
            record=_tv_snap("c-same-season", "电视剧:tmdb:1001", season=1, episodes=None, source="sub-1"),
            context=_tv_context("ShowA.S01", media_id="1001", season=1),
            downloader="qb",
            download_hash="1" * 40,
        ),
        # 异源整季刚确认 -> 不扣减但封锁 S02 避免重复提交
        ConfirmedPendingDownload(
            record=_tv_snap("c-other-season", "电视剧:tmdb:1001", season=2, episodes=None, source="other-sub"),
            context=_tv_context("ShowA.S02", media_id="1001", season=2),
            downloader="qb",
            download_hash="2" * 40,
        ),
        # 同源按集刚确认 -> 扣减 S03E01
        ConfirmedPendingDownload(
            record=_tv_snap("c-same-ep", "电视剧:tmdb:1001", season=3, episodes=(1,), source="sub-1"),
            context=_tv_context("ShowA.S03E01", media_id="1001", season=3, episodes=(1,)),
            downloader="qb",
            download_hash="3" * 40,
        ),
        # 异源按集刚确认 -> 不扣减但封锁 S03E02
        ConfirmedPendingDownload(
            record=_tv_snap("c-other-ep", "电视剧:tmdb:1001", season=3, episodes=(2,), source="other-sub"),
            context=_tv_context("ShowA.S03E02", media_id="1001", season=3, episodes=(2,)),
            downloader="qb",
            download_hash="4" * 40,
        ),
        # 目标不在 no_exists 或非电视剧 -> 安全忽略
        ConfirmedPendingDownload(
            record=_tv_snap("c-missing-media", "电视剧:tmdb:9999", season=1, episodes=None, source="sub-1"),
            context=_tv_context("Missing.S01", media_id="9999", season=1),
            downloader="qb",
            download_hash="5" * 40,
        ),
        ConfirmedPendingDownload(
            record=_tv_snap("c-missing-season", "电视剧:tmdb:1001", season=9, episodes=(1,), source="sub-1"),
            context=_tv_context("ShowA.S09E01", media_id="1001", season=9, episodes=(1,)),
            downloader="qb",
            download_hash="6" * 40,
        ),
        ConfirmedPendingDownload(
            record=download_pending_module._snapshot_from_write(
                DownloadPendingWrite(
                    pending_key="c-movie",
                    target_key="电影:tmdb:27205",
                    submitted_at="2026-11-22 21:43:28",
                    expires_at="2099-11-22 23:43:28",
                    media_type=MediaType.MOVIE.value,
                    title="Inception",
                    year="2010",
                    media_source=MediaSource.TMDB,
                    media_id="27205",
                    downloader="qb",
                    hashes=("7" * 40,),
                    torrent_name="Inception",
                    source="sub-1",
                )
            ),
            context=_movie_context("Inception.1080p"),
            downloader="qb",
            download_hash="7" * 40,
        ),
    )
    still_pending = (
        # ShowB S01 整季仍待确认，S02E01 仍待确认
        _tv_snap("p-whole", "电视剧:tmdb:1002", season=1, episodes=None, source="sub-1"),
        _tv_snap("p-ep", "电视剧:tmdb:1002", season=2, episodes=(1,), source="sub-1"),
    )

    chain = object.__new__(DownloadChain)
    chain.eventmanager = SimpleNamespace(send_event=MagicMock(return_value=None))
    chain._load_active_download_failures = MagicMock(return_value={})
    chain.reconcile_pending_downloads = MagicMock(
        return_value=PendingReconciliationResult(confirmed=confirmed, still_pending=still_pending)
    )
    chain.download_single = MagicMock(return_value=("hash-ok", "添加下载成功"))

    # ShowB S02 要求完整覆盖 [1, 2]，因 E01 待确认，整包完整集必须被拦住；S01 整季被封锁，拆包也跳过。
    ctx_b_s1 = _tv_context("ShowB.S01.1080p", media_id="1002", season=1)
    ctx_b_s1.media_info = tv_media_b
    ctx_b_s2 = _tv_context("ShowB.S02E01-E02.1080p", media_id="1002", season=2, episodes=(1, 2))
    ctx_b_s2.media_info = tv_media_b
    ctx_a_s2 = _tv_context("ShowA.S02.1080p", media_id="1001", season=2)
    ctx_a_s2.media_info = tv_media_a

    no_exists = {
        "tmdb:1001": {
            1: NotExistMediaInfo(season=1, episodes=[], total_episode=8, start_episode=1),
            2: NotExistMediaInfo(season=2, episodes=[], total_episode=8, start_episode=1),
            3: NotExistMediaInfo(season=3, episodes=[1, 2, 3], total_episode=8, start_episode=1),
        },
        "tmdb:1002": {
            1: NotExistMediaInfo(season=1, episodes=[1, 2], total_episode=8, start_episode=1),
            2: NotExistMediaInfo(
                season=2, episodes=[1, 2], total_episode=2, start_episode=1, require_complete_coverage=True
            ),
        },
        "tmdb:1003": {
            1: NotExistMediaInfo(season=1, episodes=[], total_episode=4, start_episode=1),
        },
    }
    options = download_batch._BatchDownloadOptions(
        save_path=None,
        channel=None,
        source="sub-1",
        userid=None,
        username=None,
        downloader="qb",
        custom_words=None,
        governance=None,
    )
    run = download_batch._BatchDownloadRun(chain, [ctx_a_s2, ctx_b_s1, ctx_b_s2], no_exists, options, None)
    run.run()

    # 同源确认的 S01 被移除，S03E01 被扣减；异源与待确认的季/集都被加入防重集合且无重复提交。
    assert 1 not in run.no_exists["tmdb:1001"]
    assert run.no_exists["tmdb:1001"][3].episodes == [2, 3]
    assert ("tmdb:1001", 2) in run.unconfirmed_seasons
    assert ("tmdb:1001", 3, 2) in run.unconfirmed_episodes
    assert ("tmdb:1002", 1) in run.unconfirmed_seasons
    assert ("tmdb:1002", 2, 1) in run.unconfirmed_episodes
    chain.download_single.assert_not_called()

    # 覆盖目标键前缀不匹配时的早退防御分支。
    bad_record = _tv_snap("bad", "电影:tmdb:1001", season=1, episodes=None, source="sub-1")
    run._apply_confirmed_tv_pending(bad_record, deduct_missing=True)


def test_selection_movie_music_governance_reports_confirmed_and_pending_blocks():
    """
    电影与音乐候选命中刚确认转正或仍在待确认保护期的记录时，向治理器报告 rejected 并跳过提交。
    """
    from app.application.download.pending import ConfirmedPendingDownload

    ctx_confirmed = _movie_context("Confirmed.Movie.2160p", media_id="27205")
    ctx_pending = _movie_context("Pending.Movie.2160p", media_id="27206")
    ctx_pending.media_info.title = "Interstellar"
    ctx_pending.media_info.year = "2014"
    confirmed_snap = download_pending_module._snapshot_from_write(
        DownloadPendingWrite(
            pending_key="k-confirmed",
            target_key="电影:tmdb:27205",
            submitted_at="2026-11-22 21:43:28",
            expires_at="2099-11-22 23:43:28",
            media_type=MediaType.MOVIE.value,
            title="Inception",
            year="2010",
            media_source=MediaSource.TMDB,
            media_id="27205",
            downloader="qb",
            hashes=("1" * 40,),
            torrent_name="Confirmed.Prev",
        )
    )
    pending_snap = download_pending_module._snapshot_from_write(
        DownloadPendingWrite(
            pending_key="k-pending",
            target_key="电影:tmdb:27206",
            submitted_at="2026-11-22 21:43:28",
            expires_at="2099-11-22 23:43:28",
            media_type=MediaType.MOVIE.value,
            title="Inception",
            year="2010",
            media_source=MediaSource.TMDB,
            media_id="27206",
            downloader="qb",
            hashes=("2" * 40,),
            torrent_name="Pending.Prev",
        )
    )

    decisions: list[tuple[str, str, str]] = []
    governance = SubscriptionDownloadGovernance(
        decision_observer=lambda ctx, decision, reason: decisions.append((ctx.torrent_info.title, decision, reason)),
    )
    chain = object.__new__(DownloadChain)
    chain.download_single = MagicMock()
    downloaded: list[Context] = []

    chain._download_movie_music_candidates(
        contexts=[Context(meta_info=None, media_info=None, torrent_info=None), ctx_confirmed, ctx_pending],
        downloaded_list=downloaded,
        active_failure_records={},
        save_path=None,
        channel=None,
        source=None,
        userid=None,
        username=None,
        downloader="qb",
        custom_words=None,
        governance=governance,
        pending_records=(pending_snap,),
        confirmed_pending=(
            ConfirmedPendingDownload(
                record=confirmed_snap,
                context=ctx_confirmed,
                downloader="qb",
                download_hash="1" * 40,
            ),
        ),
    )

    assert downloaded == []
    chain.download_single.assert_not_called()
    assert len(decisions) == 2
    assert decisions[0][1] == "rejected" and "已在下载器确认" in decisions[0][2]
    assert decisions[1][1] == "rejected" and "待确认下载任务" in decisions[1][2]


def test_subscribe_settle_confirmed_pending_tv_seasons_episodes_and_best_version_guard():
    """
    订阅延迟确认结算覆盖电视剧整季/按集缺集扣减、媒体库异常保守处理、洗版优先级保护与无效订阅忽略。
    """
    from app.application.download.pending import ConfirmedPendingDownload
    from app.application.mediaserver import MediaServerQueryError

    sub_tv = SubscriptionSnapshot(
        id=31,
        name="ShowA",
        year="2024",
        type=MediaType.TV.value,
        media_source=MediaSource.TMDB,
        media_id="1001",
        season=1,
    )
    sub_movie_best = SubscriptionSnapshot(
        id=32,
        name="Inception",
        year="2010",
        type=MediaType.MOVIE.value,
        media_source=MediaSource.TMDB,
        media_id="27205",
        best_version=1,
        current_priority=95,
    )
    sub_no_id = SubscriptionSnapshot(
        id=33,
        name="TitleOnly",
        year="2024",
        type=MediaType.MOVIE.value,
        media_source=None,
        media_id=None,
    )

    subs = {31: sub_tv, 32: sub_movie_best, 33: sub_no_id}
    owner = object.__new__(SubscribeChain)
    owner.subscription_repository = SimpleNamespace(get=lambda sub_id: subs.get(sub_id))
    owner.finish_subscribe_or_not = MagicMock()

    def _item(
        *,
        sub_id: int | None,
        context: Context,
        media_type: str,
        target_key: str,
        season: int | None = None,
        episodes: tuple[int, ...] | None = None,
    ) -> ConfirmedPendingDownload:
        snap = download_pending_module._snapshot_from_write(
            DownloadPendingWrite(
                pending_key=f"k-{sub_id}-{season}-{episodes}",
                target_key=target_key,
                submitted_at="2026-11-22 21:43:28",
                expires_at="2099-11-22 23:43:28",
                media_type=media_type,
                title=context.media_info.title if context.media_info else "X",
                year="2024",
                media_source=MediaSource.TMDB,
                media_id=target_key.split(":")[-1] if ":" in target_key else None,
                season=season,
                episodes=episodes,
                downloader="qb",
                hashes=("9" * 40,),
                torrent_name="Rel",
                subscription_id=sub_id,
            )
        )
        return ConfirmedPendingDownload(record=snap, context=context, downloader="qb", download_hash="9" * 40)

    # 1. 电视剧按集延迟确认：扣减 E01，保留剩余 E02。
    owner.resolve_subscribe_missing = MagicMock(
        return_value=(
            False,
            {"tmdb:1001": {1: NotExistMediaInfo(season=1, episodes=[1, 2], total_episode=2, start_episode=1)}},
        )
    )
    tv_ep_item = _item(
        sub_id=31,
        context=_tv_context("ShowA.S01E01", media_id="1001", season=1, episodes=(1,)),
        media_type=MediaType.TV.value,
        target_key="电视剧:tmdb:1001",
        season=1,
        episodes=(1,),
    )
    assert owner.settle_confirmed_pending_downloads((tv_ep_item,)) == 1
    assert owner.finish_subscribe_or_not.call_args.kwargs["lefts"]["tmdb:1001"][1].episodes == [2]

    # 2. 电视剧整季延迟确认：扣减整季后 lefts 清空。
    owner.resolve_subscribe_missing = MagicMock(
        return_value=(
            False,
            {"tmdb:1001": {1: NotExistMediaInfo(season=1, episodes=[], total_episode=2, start_episode=1)}},
        )
    )
    tv_season_item = _item(
        sub_id=31,
        context=_tv_context("ShowA.S01", media_id="1001", season=1),
        media_type=MediaType.TV.value,
        target_key="电视剧:tmdb:1001",
        season=1,
        episodes=None,
    )
    assert owner.settle_confirmed_pending_downloads((tv_season_item,)) == 1
    assert owner.finish_subscribe_or_not.call_args.kwargs["lefts"] == {}

    # 3. 媒体库查询异常时保守返回 lefts=None，不崩溃。
    owner.resolve_subscribe_missing = MagicMock(side_effect=MediaServerQueryError("jellyfin down"))
    assert owner.settle_confirmed_pending_downloads((tv_season_item,)) == 1
    assert owner.finish_subscribe_or_not.call_args.kwargs["lefts"] is None

    # 4. 电影洗版已有更高优先级（95 >= 90），旧待确认回执被忽略不降级。
    low_pri_movie = _item(
        sub_id=32,
        context=_movie_context("Inception.Old", media_id="27205", pri_order=90),
        media_type=MediaType.MOVIE.value,
        target_key="电影:tmdb:27205",
    )
    assert owner.settle_confirmed_pending_downloads((low_pri_movie,)) == 0

    # 5. 无订阅 ID、订阅已删、缺少 meta/media、身份不匹配均安全返回 0；无媒体 ID 时按标题匹配。
    no_sub_item = _item(
        sub_id=None,
        context=_movie_context("NoSub"),
        media_type=MediaType.MOVIE.value,
        target_key="电影:tmdb:27205",
    )
    deleted_sub_item = _item(
        sub_id=999,
        context=_movie_context("DeletedSub"),
        media_type=MediaType.MOVIE.value,
        target_key="电影:tmdb:27205",
    )
    broken_ctx_item = _item(
        sub_id=31,
        context=Context(meta_info=None, media_info=None, torrent_info=TorrentInfo(title="Broken")),
        media_type=MediaType.TV.value,
        target_key="电视剧:tmdb:1001",
    )
    mismatch_item = _item(
        sub_id=31,
        context=_tv_context("OtherShow.S01", media_id="8888", season=1),
        media_type=MediaType.TV.value,
        target_key="电视剧:tmdb:8888",
    )
    title_only_ctx = Context(
        meta_info=MetaInfo("TitleOnly.2024"),
        media_info=MediaInfo(title="TitleOnly", year="2024", type=MediaType.MOVIE),
        torrent_info=TorrentInfo(title="TitleOnly.1080p", pri_order=80),
    )
    title_only_item = _item(
        sub_id=33,
        context=title_only_ctx,
        media_type=MediaType.MOVIE.value,
        target_key="电影:TitleOnly:2024",
    )
    assert owner.settle_confirmed_pending_downloads(
        (no_sub_item, deleted_sub_item, broken_ctx_item, mismatch_item, title_only_item)
    ) == 1
