from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.chain.subscribe import SubscribeChain, build_subscribe_meta
from app.core.context import MUSIC_ENTITY_ALBUM, MUSIC_ENTITY_RECORDING, Context, TorrentInfo
from app.core.meta import MetaMusic
from app.core.context import MusicInfo
from app.schemas.types import MediaType


def _music_info() -> MusicInfo:
    """构造音乐订阅测试使用的标准目标。"""
    return MusicInfo(
        source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
    )


def _subscribe(**overrides) -> SimpleNamespace:
    """构造不依赖数据库的音乐订阅对象。"""
    values = dict(
        id=7,
        name="晴天",
        year="2003",
        type=MediaType.MUSIC.value,
        keyword=None,
        media_source="musicbrainz",
        media_id="recording-1",
        music_type="recording",
        total_tracks=None,
        season=None,
        episode_group=None,
        tmdbid=None,
        imdbid=None,
        tvdbid=None,
        doubanid=None,
        bangumiid=None,
        anilistid=None,
        sites=[],
        filter_groups=[],
        quality=None,
        resolution=None,
        effect=None,
        include=None,
        exclude=None,
        username="admin",
        save_path=None,
        downloader=None,
        custom_words=None,
        media_category=None,
        best_version=0,
        state="R",
        note=None,
        poster=None,
        backdrop=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_subscribe_meta_returns_music_meta():
    """音乐订阅应构造 MetaMusic，而不是交给影视标题解析器。"""
    meta = build_subscribe_meta(_subscribe())

    assert isinstance(meta, MetaMusic)
    assert meta.type == MediaType.MUSIC
    assert meta.media_id == "recording-1"
    assert meta.original_name == "晴天"


def test_music_subscribe_reuses_search_download_and_finish_flow():
    """音乐订阅应复用站点搜索、批量下载和订阅完成主流程。"""
    subscribe = _subscribe()
    target = _music_info()
    context = Context(
        torrent_info=TorrentInfo(
            title="周杰伦 - 晴天 FLAC",
            category=MediaType.MUSIC.value,
        )
    )
    search_chain = Mock()
    search_chain.search_by_title.return_value = [context]
    download_chain = Mock()
    download_chain.batch_download.return_value = ([context], None)
    chain = SubscribeChain()
    chain.finish_subscribe_or_not = Mock()

    with patch.object(SubscribeChain, "_recognize_music_subscribe", return_value=target), \
            patch("app.chain.subscribe.SearchChain", return_value=search_chain), \
            patch("app.chain.subscribe.DownloadChain", return_value=download_chain), \
            patch("app.chain.subscribe.SubscribeOper") as subscribe_oper:
        subscribe_oper.return_value.get.return_value = subscribe
        chain._search_music_subscribe(subscribe)

    search_chain.search_by_title.assert_called_once_with(
        title="周杰伦 晴天",
        sites=[],
        mtype=MediaType.MUSIC,
        rule_groups=[],
    )
    assert context.media_info is target
    assert isinstance(context.meta_info, MetaMusic)
    assert context.meta_info.org_string == "周杰伦 - 晴天 FLAC"
    download_chain.batch_download.assert_called_once()
    chain.finish_subscribe_or_not.assert_called_once()


def test_music_subscribe_ignores_non_music_category():
    """音乐订阅不得自动下载未被站点分类为音乐的资源。"""
    subscribe = _subscribe()
    context = Context(
        torrent_info=TorrentInfo(
            title="周杰伦 - 叶惠美 FLAC",
            category=MediaType.MOVIE.value,
        )
    )
    search_chain = Mock()
    search_chain.search_by_title.return_value = [context]
    chain = SubscribeChain()

    with patch.object(SubscribeChain, "_recognize_music_subscribe", return_value=_music_info()), \
            patch("app.chain.subscribe.SearchChain", return_value=search_chain), \
            patch("app.chain.subscribe.DownloadChain") as download_chain:
        chain._search_music_subscribe(subscribe)

    download_chain.assert_not_called()


def test_music_subscribe_ignores_unrelated_music_title():
    """即使站点分类为音乐，资源标题不含目标单曲或专辑名时也不得自动下载。"""
    subscribe = _subscribe()
    context = Context(
        torrent_info=TorrentInfo(
            title="周杰伦 - 七里香 FLAC",
            category=MediaType.MUSIC.value,
        )
    )
    search_chain = Mock()
    search_chain.search_by_title.return_value = [context]

    with patch.object(SubscribeChain, "_recognize_music_subscribe", return_value=_music_info()), \
            patch("app.chain.subscribe.SearchChain", return_value=search_chain), \
            patch("app.chain.subscribe.DownloadChain") as download_chain:
        SubscribeChain()._search_music_subscribe(subscribe)

    download_chain.assert_not_called()


def test_album_subscription_uses_persisted_snapshot_when_remote_detail_is_unavailable():
    """远端详情短暂失败时应从订阅快照恢复专辑语义，不能按标题猜成第一首单曲。"""
    subscribe = _subscribe(
        name="叶惠美",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        total_tracks=11,
    )
    media_chain = Mock()
    media_chain.recognize_media.return_value = None

    with patch("app.chain.subscribe.MediaChain", return_value=media_chain), \
            patch("app.chain.subscribe.MusicChain.search") as search:
        restored = SubscribeChain._recognize_music_subscribe(subscribe)

    assert restored.music_type == MUSIC_ENTITY_ALBUM
    assert restored.album == "叶惠美"
    assert restored.total_tracks == 11
    search.assert_not_called()


def test_legacy_music_identity_failure_does_not_guess_entity_from_title():
    """旧订阅有标准 ID 却无实体类型时，识别失败后应保留订阅而不是误选标题搜索首项。"""
    subscribe = _subscribe(music_type=None)
    media_chain = Mock()
    media_chain.recognize_media.return_value = None

    with patch("app.chain.subscribe.MediaChain", return_value=media_chain), \
            patch("app.chain.subscribe.MusicChain.search") as search:
        restored = SubscribeChain._recognize_music_subscribe(subscribe)

    assert restored is None
    search.assert_not_called()


def test_album_subscription_without_remote_id_uses_persisted_entity_snapshot():
    """专辑快照缺少远端 ID 时也不得退化为单曲识别。"""
    subscribe = _subscribe(
        name="叶惠美",
        media_source=None,
        media_id=None,
        music_type=MUSIC_ENTITY_ALBUM,
        total_tracks=11,
    )

    with patch("app.chain.subscribe.MediaChain") as media_chain:
        restored = SubscribeChain._recognize_music_subscribe(subscribe)

    assert restored.music_type == MUSIC_ENTITY_ALBUM
    assert restored.total_tracks == 11
    media_chain.assert_not_called()


def test_legacy_music_without_identity_uses_recording_recognition_boundary():
    """旧订阅缺少标准身份时只能恢复为单曲，不能消费全局搜索中的专辑或艺术家候选。"""
    subscribe = _subscribe(
        media_source=None,
        media_id=None,
        music_type=None,
    )
    recording = _music_info()
    media_chain = Mock()
    media_chain.recognize_media.return_value = recording

    with patch("app.chain.subscribe.MediaChain", return_value=media_chain), \
            patch("app.chain.subscribe.MusicChain.search") as mixed_search:
        restored = SubscribeChain._recognize_music_subscribe(subscribe)

    assert restored is recording
    mixed_search.assert_not_called()
    media_chain.recognize_media.assert_called_once()
    call = media_chain.recognize_media.call_args
    assert isinstance(call.kwargs["meta"], MetaMusic)
    assert call.kwargs["mtype"] == MediaType.MUSIC


def test_album_subscription_finishes_only_after_confirmed_full_pack():
    """专辑与电视剧全集相同，必须确认整专覆盖；单曲仍在任一成功下载后完成。"""
    album_subscribe = _subscribe(music_type=MUSIC_ENTITY_ALBUM, total_tracks=11)
    album = MusicInfo(
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        album="叶惠美",
        total_tracks=11,
    )

    assert SubscribeChain._is_music_download_complete(
        album_subscribe,
        album,
        [Context(confirmed_full_coverage=False)],
    ) is False
    assert SubscribeChain._is_music_download_complete(
        album_subscribe,
        album,
        [Context(confirmed_full_coverage=True)],
    ) is True
    assert SubscribeChain._is_music_download_complete(
        _subscribe(),
        _music_info(),
        [Context()],
    ) is True


def test_music_subscribe_target_validation_enforces_entity_semantics():
    """单曲无需专辑曲目数，专辑必须有总曲目数，艺术家和实体错配均不可订阅。"""
    recording = _music_info()
    recording.total_tracks = 11
    album = MusicInfo(
        source="musicbrainz",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        album="叶惠美",
        total_tracks=11,
    )

    assert SubscribeChain._validate_music_subscribe_target(
        recording, MUSIC_ENTITY_RECORDING
    ) is None
    assert SubscribeChain._validate_music_subscribe_target(album, MUSIC_ENTITY_ALBUM) is None
    assert "类型不匹配" in (
        SubscribeChain._validate_music_subscribe_target(album, MUSIC_ENTITY_RECORDING) or ""
    )
    album.total_tracks = None
    assert "总曲目数未知" in (
        SubscribeChain._validate_music_subscribe_target(album, MUSIC_ENTITY_ALBUM) or ""
    )
    assert "仅支持单曲或专辑" in (
        SubscribeChain._validate_music_subscribe_target(recording, "artist") or ""
    )


def test_recording_target_sync_clears_stale_album_track_count():
    """旧单曲订阅若误存所属专辑曲目数，刷新元数据时应主动清空。"""
    subscribe = _subscribe(total_tracks=11)
    subscribe_oper = Mock()

    with patch("app.chain.subscribe.SubscribeOper", return_value=subscribe_oper):
        SubscribeChain._sync_music_subscribe_target(subscribe, _music_info())

    subscribe_oper.update.assert_called_once_with(subscribe.id, {"total_tracks": None})
    assert subscribe.total_tracks is None


def test_subscribe_add_music_uses_unified_recognize_by_meta():
    """音乐订阅新增应走统一 recognize_by_meta，并把媒体身份落到 MetaMusic 上。"""
    target = _music_info()
    media_chain = Mock()
    media_chain.recognize_by_meta = Mock(return_value=target)
    subscribe_oper = Mock()
    subscribe_oper.add.return_value = (1, "")

    with patch("app.chain.subscribe.MediaChain", return_value=media_chain), \
            patch("app.chain.subscribe.SubscribeOper", return_value=subscribe_oper), \
            patch("app.chain.subscribe.MoviePilotServerHelper"), \
            patch("app.chain.subscribe.eventmanager"):
        sid, err_msg = SubscribeChain().add(
            title="周杰伦 - 晴天",
            year="2003",
            mtype=MediaType.MUSIC,
            media_source="musicbrainz",
            media_id="recording-1",
            message=False,
        )

    assert sid == 1
    assert err_msg == ""
    media_chain.recognize_by_meta.assert_called_once()
    routed_meta = media_chain.recognize_by_meta.call_args.args[0]
    assert isinstance(routed_meta, MetaMusic)
    # 媒体身份落到 meta，供统一识别的详情分支复用
    assert routed_meta.media_id == "recording-1"
    assert media_chain.recognize_by_meta.call_args.kwargs["source"] == "musicbrainz"


def test_subscribe_add_rejects_music_entity_mismatch_before_database_write():
    """请求专辑却识别为单曲时必须中止，不能创建完成语义错误的订阅。"""
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = _music_info()
    subscribe_oper = Mock()

    with patch("app.chain.subscribe.MediaChain", return_value=media_chain), \
            patch("app.chain.subscribe.SubscribeOper", return_value=subscribe_oper):
        sid, err_msg = SubscribeChain().add(
            title="叶惠美",
            year="2003",
            mtype=MediaType.MUSIC,
            media_source="musicbrainz",
            media_id="recording-1",
            music_type=MUSIC_ENTITY_ALBUM,
            message=False,
        )

    assert sid is None
    assert "类型不匹配" in err_msg
    subscribe_oper.add.assert_not_called()


def test_subscribe_add_music_fails_fast_on_offline_fallback():
    """统一识别返回离线兜底（无远端 source）时订阅应直接失败，不写入数据库。"""
    offline = MusicInfo(title="未知曲目", artists=["未知艺术家"])
    media_chain = Mock()
    media_chain.recognize_by_meta = Mock(return_value=offline)
    subscribe_oper = Mock()
    subscribe_oper.add.return_value = (1, "")

    with patch("app.chain.subscribe.MediaChain", return_value=media_chain), \
            patch("app.chain.subscribe.SubscribeOper", return_value=subscribe_oper):
        sid, err_msg = SubscribeChain().add(
            title="未知曲目",
            year=None,
            mtype=MediaType.MUSIC,
            message=False,
        )

    assert sid is None
    assert err_msg == "未识别到媒体信息"
    subscribe_oper.add.assert_not_called()


def test_follow_preserves_album_entity_and_track_count():
    """Follow 专辑分享不得走影视标题解析，并须保留整专完成判定字段。"""
    share = {
        "share_uid": "follow-user",
        "name": "叶惠美",
        "type": MediaType.MUSIC.value,
        "year": "2003",
        "media_source": "musicbrainz",
        "media_id": "release-group-1",
        "music_type": MUSIC_ENTITY_ALBUM,
        "total_tracks": 11,
        "filter_groups": [],
    }
    subscribe_oper = Mock()
    subscribe_oper.exists.return_value = False
    subscribe_oper.exist_history.return_value = False
    system_config = Mock()
    system_config.get.return_value = ["follow-user"]

    with patch("app.chain.subscribe.SubscribeOper", return_value=subscribe_oper), \
            patch("app.chain.subscribe.SystemConfigOper", return_value=system_config), \
            patch(
                "app.chain.subscribe.MoviePilotServerHelper.get_subscribe_shares",
                return_value=[share],
            ), \
            patch("app.chain.subscribe.MetaInfo") as video_meta, \
            patch.object(SubscribeChain, "add", return_value=(1, "")) as add:
        SubscribeChain.follow()

    video_meta.assert_not_called()
    assert subscribe_oper.exists.call_args.kwargs["music_type"] == MUSIC_ENTITY_ALBUM
    assert subscribe_oper.exist_history.call_args.kwargs["music_type"] == MUSIC_ENTITY_ALBUM
    assert add.call_args.kwargs["music_type"] == MUSIC_ENTITY_ALBUM
    assert add.call_args.kwargs["total_tracks"] == 11
