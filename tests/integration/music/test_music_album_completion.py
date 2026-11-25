"""
专辑发行身份、入库覆盖和重复提交的回归测试。
"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from torrentool.bencode import Bencode

from app.application.subscription.contract import SubscriptionSnapshot
from app.application.torrent.download import TorrentHelper
from app.chain.download import DownloadChain
from app.chain.download.submission import _PreparedDownload
from app.chain.subscribe.completion import _record_music_download_facts
from app.chain.subscribe.facade import SubscribeChain
from app.chain.subscribe.music import _read_music_album_evidence
from app.domain.context import Context, MusicInfo, TorrentInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import music_completed_album_keys, music_subscription_album_matches
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaSource, MediaType


@pytest.fixture
def album():
    """
    目标发行版总数高于实际标准版，但保留正确的发行组身份。
    """
    return MusicInfo(media_source=MediaSource.MusicBrainz, media_id="group", music_type=MUSIC_ENTITY_ALBUM,
                     title="叶惠美", album="叶惠美", artists=["周杰伦"], total_tracks=100,
                     musicbrainz_release_group_id="group", musicbrainz_release_id="deluxe")


def _track(number, *, total=3, disc=1, discs=1, release="standard"):
    """
    构造有明确盘数、曲序和发行身份的真实标签证据。
    """
    return MetaMusic(title=f"曲目{number}", album="叶惠美", artists=["周杰伦"], track_number=number,
                     total_tracks=total, disc_number=disc, total_discs=discs,
                     musicbrainz_release_id=release, musicbrainz_release_group_id="group")


@pytest.mark.parametrize("changes,expected", [
    ({"media_id": "release", "musicbrainz_release_id": "standard"}, True),
    ({"musicbrainz_release_group_id": "other"}, False),
    ({"album": "Sound & Sight"}, False),
    ({"album": None, "title": "Sound & Sight"}, False),
    ({"music_type": "recording"}, False),
])
def test_album_identity_checks_group_and_title(album, changes, expected):
    """
    同发行组可跨发行，冲突的组、专辑名和音乐实体不能借身份相同串扰。
    """
    assert music_subscription_album_matches(album, replace(album, **changes)) is expected


def test_album_identity_fallback_requires_artist_and_album(album):
    """
    缺失组身份时须同时核验专辑和艺人，不能只按同名歌曲放行。
    """
    candidate = MusicInfo(music_type=MUSIC_ENTITY_ALBUM, title="葉惠美", album="葉惠美", artists=["周杰倫"])
    assert music_subscription_album_matches(album, candidate)
    candidate.artists = ["其他艺人"]
    assert not music_subscription_album_matches(album, candidate)
    candidate.artists = []
    assert not music_subscription_album_matches(album, candidate)


@pytest.mark.parametrize("metas,expected", [
    ([_track(1), _track(2), _track(3)], ["[1,1]", "[1,2]", "[1,3]"]),
    ([_track(1), _track(3)], []),
    ([_track(1), _track(1), _track(2)], []),
    ([_track(1, total=None)], []),
    ([_track(1, discs=None), _track(2, discs=None), _track(3, discs=None)], []),
    ([_track(1, total=1, discs=2)], []),
    ([_track(1, total=1, disc=1, discs=2), _track(1, total=1, disc=2, discs=2)], ["[1,1]", "[2,1]"]),
    ([_track(1, total=1), _track(2, total=2)], []),
    ([_track(1), _track(2, release="other"), _track(3)], []),
])
def test_actual_release_requires_complete_per_disc_coverage(album, metas, expected):
    """
    数量不足、缺盘、重复、未知总数和跨发行拼接都不能收紧完成上界。
    """
    assert music_completed_album_keys(metas, album) == expected


def test_image_cue_counts_logical_tracks(album):
    """
    整轨音频使用有效 CUE 的逻辑曲目覆盖，不按一个物理文件计算完成。
    """
    image = _track(1)
    image.music_layout = "image_cue"
    image.cue_tracks = [{"number": number, "start_frame": (number - 1) * 7500, "title": f"曲目{number}"}
                        for number in range(1, 4)]
    assert music_completed_album_keys([image], album) == ["[1,1]", "[1,2]", "[1,3]"]
    image.cue_tracks[1]["start_frame"] = 0
    assert music_completed_album_keys([image], album) == []


def test_history_recovery_completes_actual_release_across_hashes(album, monkeypatch):
    """
    旧下载关联的成功入库证据可跨 hash 补记，并以完整实际发行版结束普通订阅。
    """
    chain = SubscribeChain()
    subscribe = SubscriptionSnapshot(id=7, name=album.title, type=MediaType.MUSIC.value,
                                     media_source=album.media_source, media_id=album.media_id,
                                     music_type=MUSIC_ENTITY_ALBUM, total_tracks=100, downloaded_tracks=["[9,9]"],
                                     state="R", username="alice", sites=[1], save_path="/downloads")
    chain.download_history_repository = Mock()
    chain.download_history_repository.get_by_media_identity.return_value = [
        SimpleNamespace(download_hash="hash-a"), SimpleNamespace(download_hash="hash-b"),
    ]
    chain.transfer_history_repository = Mock()
    histories = [SimpleNamespace(status=True, type=MediaType.MUSIC.value, dest_storage="local", dest=f"/library/{i}.flac")
                 for i in range(1, 4)]
    chain.transfer_history_repository.list_by_hash.side_effect = [histories[:1], histories[1:]]
    monkeypatch.setattr("app.chain.subscribe.music._read_music_album_evidence",
                        lambda dest, _target: _track(int(Path(dest).stem)))
    update = Mock(side_effect=lambda old, payload, **_kwargs: replace(old, **payload))
    chain._SubscribeChain__apply_subscribe_update = update
    chain._SubscribeChain__update_subscribe_note = Mock()
    chain._SubscribeChain__finish_subscribe = Mock()
    chain.resolve_subscribe_missing = Mock()

    exists, _ = chain.check_and_handle_existing_media(subscribe, MetaMusic.from_music_info(album), album, "musicbrainz:group")

    assert exists
    finished = chain._SubscribeChain__finish_subscribe.call_args.kwargs["subscribe"]
    assert finished.total_tracks == 3
    assert finished.downloaded_tracks == ["[1,1]", "[1,2]", "[1,3]"]
    assert finished.username == "alice" and finished.sites == [1] and finished.save_path == "/downloads"
    chain.resolve_subscribe_missing.assert_not_called()
    assert update.call_args.kwargs["scene"] == "music_download_tracks"


def test_history_rejects_failed_remote_and_missing_tag_evidence(album, monkeypatch):
    """
    失败整理、远端路径和无真实标签的本地路径不会贡献曲目。
    """
    chain = SubscribeChain()
    chain.transfer_history_repository = Mock()
    chain.transfer_history_repository.list_by_hash.return_value = [
        SimpleNamespace(status=False, type=MediaType.MUSIC.value, dest_storage="local", dest="/a.flac"),
        SimpleNamespace(status=True, type=MediaType.MUSIC.value, dest_storage="remote", dest="/b.flac"),
        SimpleNamespace(status=True, type=MediaType.MUSIC.value, dest_storage="local", dest="/missing.flac"),
    ]
    read = Mock(return_value=(MetaMusic(title="03"), None, MetaMusic(title="03")))
    monkeypatch.setattr("app.chain.subscribe.music.AudioMetadataHelper.read_evidence", read)
    assert chain._music_album_history_contexts(["hash"], album) == []
    read.assert_called_once_with(Path("/missing.flac"))
    assert _read_music_album_evidence(None, album) is None


def test_mismatched_album_cannot_update_progress_or_upgrade(album):
    """
    持久化合并和洗版预判共享身份门禁；跨专辑键即使数量足够也无效。
    """
    subscribe = SubscriptionSnapshot(id=7, name=album.title, music_type=MUSIC_ENTITY_ALBUM,
                                     total_tracks=3, downloaded_tracks=[])
    other = replace(album, musicbrainz_release_group_id="other")
    context = Context(media_info=other, music_track_keys=["[1,1]", "[1,2]", "[1,3]"])
    update = Mock()
    assert _record_music_download_facts(update, Mock(), subscribe, [context], album) is subscribe
    update.assert_not_called()
    assert not SubscribeChain._music_downloads_complete_after_merge(subscribe, album, [context])


@pytest.mark.parametrize("content,expected", [
    ("magnet:?xt=urn:btih:" + "A" * 40, "a" * 40),
    (b"invalid torrent", None),
    ("magnet:?xt=urn:btih:bad", None),
])
def test_actual_torrent_hash_parser(content, expected):
    """
    磁力身份归一化，无法解析的内容保持原下载流程而不触发误去重。
    """
    assert TorrentHelper.get_torrent_hash(content) == expected


def test_hash_guard_skips_only_fully_transferred_subscription(album, tmp_path):
    """
    跨轮次去重以种子真实 hash 和全部音频成功整理为准，保留人工下载与失败重试。

    交付事实来自整理历史：单曲被移出媒体库不重投整包，整理目标全部消失才视为交付失效。
    """
    content = Bencode.encode({"info": {"name": "album", "piece length": 16384, "pieces": "x" * 20, "length": 100}})
    actual_hash = TorrentHelper.get_torrent_hash(content)
    prepared = _PreparedDownload(torrent=TorrentInfo(title="专辑"), media=album,
                                 meta=MetaMusic.from_music_info(album), torrent_content=content,
                                 folder_name="album", file_list=["01.flac", "02.flac"], download_dir=Path("/downloads"),
                                 download_uri="file:///downloads", download_episodes=None, site_downloader=None)
    chain = DownloadChain()
    chain.download_history_repository = Mock()
    chain.transfer_history_repository = Mock()
    chain.download_history_repository.get_by_hash.return_value = SimpleNamespace(
        media_source=album.media_source, media_id=album.media_id, downloader="qb")
    chain.download_history_repository.get_files_by_hash.return_value = [
        SimpleNamespace(fullpath=f"/downloads/{i}.flac") for i in (1, 2)]
    for number in (1, 2):
        (tmp_path / f"{number}.flac").write_bytes(b"audio")
    success = [SimpleNamespace(src=f"/downloads/{i}.flac", dest=str(tmp_path / f"{i}.flac"), status=True,
                               dest_storage="local",
                               type=MediaType.MUSIC.value, downloader="qb", src_storage="local") for i in (1, 2)]
    chain.transfer_history_repository.list_by_hash.return_value = success
    assert chain._has_completed_music_download(prepared, "Subscribe|7")
    chain.download_history_repository.get_by_hash.assert_called_with(actual_hash)
    assert not chain._has_completed_music_download(prepared, "Manual")
    (tmp_path / "2.flac").unlink()
    assert chain._has_completed_music_download(prepared, "Subscribe|7")
    (tmp_path / "1.flac").unlink()
    assert not chain._has_completed_music_download(prepared, "Subscribe|7")
    for number in (1, 2):
        (tmp_path / f"{number}.flac").write_bytes(b"audio")
    # 同一曲目的过期整理记录不会抵消仍然在库的那份交付。
    success.append(SimpleNamespace(src="/downloads/1.flac", dest=str(tmp_path / "stale.flac"), status=True,
                                   dest_storage="local", type=MediaType.MUSIC.value, downloader="qb",
                                   src_storage="local"))
    assert chain._has_completed_music_download(prepared, "Subscribe|7")
    success.pop()
    success[1].status = False
    assert not chain._has_completed_music_download(prepared, "Subscribe|7")
    success[0].status = False
    # 没有任何有效整理记录时交付事实为空，必须保留失败重试。
    assert not chain._has_completed_music_download(prepared, "Subscribe|7")
    success[0].status = success[1].status = True
    success[1].downloader = "other"
    assert not chain._has_completed_music_download(prepared, "Subscribe|7")
    chain.download_history_repository.get_by_hash.return_value.media_id = "other"
    assert not chain._has_completed_music_download(prepared, "Subscribe|7")


def test_duplicate_guard_precedes_downloader_and_notifications(album, monkeypatch):
    """
    已完整整理的资源不得调用下载器、写重复历史、通知或提前标记下载已开始。
    """
    chain = DownloadChain()
    prepared = SimpleNamespace(torrent=TorrentInfo(title="专辑"))
    monkeypatch.setattr(chain, "_prepare_download_single", Mock(return_value=(prepared, None)))
    monkeypatch.setattr(chain, "_has_completed_music_download", Mock(return_value=True))
    submit = Mock()
    monkeypatch.setattr(chain, "_submit_prepared_download", submit)
    governance = Mock()
    result, reason = chain._execute_download_single(Context(media_info=album), source="Subscribe|7",
                                                   governance=governance, return_detail=True)
    assert result is None and "跳过重复提交" in reason
    submit.assert_not_called()
    governance.mark_started.assert_not_called()
    assert governance.report.call_args.args[1] == "rejected"


def test_album_progress_uses_durable_mutation_and_preserves_attached_fields(album, db):
    """
    真实事务保存完成上界及曲目事实，并保留数据库较新的附加字段与 durable 修改事件。
    """
    from contextlib import contextmanager

    from sqlalchemy import select

    from app.application.subscription.mutation import SyncSubscriptionMutationService
    from app.db.adapters.outbox import SqlAlchemyOutboxDispatchStore, SqlAlchemyOutboxStager
    from app.db.adapters.subscription import SessionSubscriptionRepository
    from app.db.models.outbox import OutboxMessage
    from app.db.models.subscribe import Subscribe
    from app.db.session import SessionFactory
    from app.db.uow import SqlAlchemyUnitOfWork

    db.watermark(Subscribe, OutboxMessage)
    row = db.add(Subscribe(name=album.title, type=MediaType.MUSIC.value, music_type=MUSIC_ENTITY_ALBUM,
                           media_source=album.media_source.value, media_id=album.media_id, total_tracks=100,
                           downloaded_tracks=[], sites=[1, 2], filter_groups=["Lossless"], username="alice",
                           save_path="/downloads", state="R", best_version=0))
    chain = SubscribeChain()
    with SessionFactory() as session:
        original = SessionSubscriptionRepository(session).get(row.id)
    published = []

    @contextmanager
    def mutation_scope():
        """
        沿用生产短事务及 outbox，事件投递仅在测试内捕获。
        """
        with SessionFactory() as session:
            yield SyncSubscriptionMutationService(
                repository=SessionSubscriptionRepository(session), unit_of_work=SqlAlchemyUnitOfWork(session),
                outbox=SqlAlchemyOutboxStager(session), dispatch_store=SqlAlchemyOutboxDispatchStore(SessionFactory),
                publish_modified=published.append,
            )

    chain.sync_subscription_mutation_scope = mutation_scope
    updated = _record_music_download_facts(
        chain._SubscribeChain__apply_subscribe_update, chain._SubscribeChain__update_subscribe_note,
        replace(original, sites=[99], filter_groups=[]),
        [Context(media_info=album, music_track_keys=["[1,1]", "[1,2]", "[1,3]"], confirmed_full_coverage=True)], album,
    )
    db.session.expire_all()
    stored = db.session.get(Subscribe, row.id)
    assert stored.total_tracks == 3 and stored.downloaded_tracks == ["[1,1]", "[1,2]", "[1,3]"]
    assert stored.sites == [1, 2] and stored.filter_groups == ["Lossless"]
    assert stored.username == "alice" and stored.save_path == "/downloads" and stored.state == "R"
    assert updated.sites == [1, 2]
    assert len(published) == 1 and published[0]["scene"] == "music_download_tracks"
    assert published[0]["subscribe_info"]["total_tracks"] == 3
    events = db.session.execute(select(OutboxMessage).where(OutboxMessage.topic == "subscribe.modified")).scalars().all()
    matching = [event for event in events if event.payload.get("subscribe_id") == row.id]
    assert len(matching) == 1 and matching[0].payload["subscribe_info"]["downloaded_tracks"] == stored.downloaded_tracks


def test_different_release_positions_cannot_fabricate_completion(album):
    """
    不同发行版的编号不能与旧进度拼凑，只有实际完整覆盖证明可替换上界。
    """
    target = replace(album, total_tracks=3)
    subscribe = SubscriptionSnapshot(id=7, name=album.title, music_type=MUSIC_ENTITY_ALBUM,
                                     total_tracks=3, downloaded_tracks=["[1,1]", "[1,2]"])
    alternate = replace(album, musicbrainz_release_id="standard")
    context = Context(media_info=alternate, music_track_keys=["[1,3]"])
    update = Mock()
    assert _record_music_download_facts(update, Mock(), subscribe, [context], target) is not None
    assert update.call_args.args[1]["downloaded_tracks"] == ["[1,1]", "[1,2]"]
    assert not SubscribeChain._music_downloads_complete_after_merge(subscribe, target, [context])
    context.music_track_keys = ["[1,1]", "[1,2]"]
    context.confirmed_full_coverage = True
    update.side_effect = lambda old, payload, **_kwargs: replace(old, **payload)
    recovered = _record_music_download_facts(update, Mock(), subscribe, [context], target)
    assert recovered.total_tracks == 2 and recovered.downloaded_tracks == ["[1,1]", "[1,2]"]
