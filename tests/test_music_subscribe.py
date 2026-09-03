import asyncio
from contextlib import contextmanager
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.application.subscription.contract import (
    SubscriptionIdentity,
    SubscriptionPatch,
    SubscriptionSnapshot,
    build_subscribe_meta,
)
from app.application.subscription.execution import (
    SubscriptionExecutionAdmission,
    SubscriptionExecutionContext,
)
from app.application.subscription.mutation import SubscriptionMutation
from app.application.subscription.sitebudget import SubscriptionSearchCancelled
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_ARTIST,
    MUSIC_ENTITY_RECORDING,
    Context,
    MusicInfo,
    TorrentInfo,
)
from app.domain.meta.metamusic import MetaMusic
from app.schemas.category import ClassificationSelection
from app.schemas.types import MediaSource, MediaType


def _music_info() -> MusicInfo:
    """构造音乐订阅测试使用的标准目标。"""
    return MusicInfo(
        media_source="musicbrainz",
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
        mediaid=None,
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
        audio_quality=None,
        audio_format=None,
        min_bitrate=None,
        min_bit_depth=None,
        min_sample_rate=None,
        include=None,
        exclude=None,
        username="admin",
        save_path=None,
        downloader=None,
        custom_words=None,
        media_category=None,
        best_version=0,
        best_version_full=0,
        current_priority=None,
        current_audio_format=None,
        current_bitrate=None,
        current_bit_depth=None,
        current_sample_rate=None,
        state="R",
        note=None,
        description=None,
        poster=None,
        backdrop=None,
    )
    values.update(overrides)
    subscribe = SimpleNamespace(**values)
    subscribe.to_dict = lambda: dict(values)
    return subscribe


def _music_recognition_chain(
        recognized: MusicInfo | None = None,
) -> Mock:
    """构造同时支持同步、异步识别与分类收口的媒体链替身。"""
    media_chain = Mock()
    media_chain.recognize_media.return_value = recognized
    media_chain.async_recognize_media = AsyncMock(return_value=recognized)
    media_chain._finalize_recognition_result.side_effect = (
        lambda mediainfo, **_kwargs: mediainfo
    )
    return media_chain


def _configure_subscription_write(chain, repository) -> None:
    """为音乐测试链注入显式同步修改作用域。"""
    chain.subscription_repository = repository

    @contextmanager
    def mutation_scope():
        """把同步修改命令委托给当前测试 repository。"""
        def update(subscribe_id, payload, _actor, existing=None, scene="update"):
            """按生产命令返回结构记录测试写入。"""
            repository.update(subscribe_id, SubscriptionPatch(payload))
            old = {
                field.name: getattr(existing, field.name, None)
                for field in fields(SubscriptionSnapshot)
            }
            new = {**old, **payload}
            return SubscriptionMutation(
                snapshot=SubscriptionSnapshot(**new),
                old=old,
                new=new,
            )

        yield SimpleNamespace(update=update)

    chain.sync_subscription_mutation_scope = mutation_scope


def _execution_context(*, cancelled=None) -> SubscriptionExecutionContext:
    """构造音乐订阅使用的独立执行上下文。"""
    admission = SubscriptionExecutionAdmission()
    lease = admission.try_acquire(
        subscription_id=7,
        operation="search",
        ttl_seconds=60,
    )
    assert lease is not None
    return SubscriptionExecutionContext(
        lease=lease,
        admission=admission,
        task_id="music-task-7",
        cancel_requested=cancelled,
    )


def test_build_subscribe_meta_returns_music_meta():
    """音乐订阅应构造 MetaMusic，而不是交给影视标题解析器。"""
    meta = build_subscribe_meta(_subscribe())

    assert isinstance(meta, MetaMusic)
    assert meta.type == MediaType.MUSIC
    assert meta.media_id == "recording-1"
    assert meta.original_name == "晴天"


def test_music_subscribe_recovers_completion_from_persisted_download_note():
    """进程重启后音乐订阅应像电影一样用已确认下载备注恢复完成状态。"""
    subscribe = _subscribe(note=[1])
    subscribe.total_episode = 0
    subscribe.manual_total_episode = False
    download_chain = Mock()
    download_chain.get_no_exists_info.return_value = (False, {})

    with patch("app.chain.subscribe.refresh.DownloadChain", return_value=download_chain):
        satisfied, no_exists = SubscribeChain().resolve_subscribe_missing(
            subscribe=subscribe,
            meta=build_subscribe_meta(subscribe),
            mediainfo=_music_info(),
        )

    assert satisfied is True
    assert no_exists == {}


def test_music_subscribe_reuses_search_download_and_finish_flow():
    """音乐订阅应复用站点搜索、批量下载和订阅完成主流程。"""
    subscribe = _subscribe(keyword="周杰伦 晴天")
    target = _music_info()
    context = Context(
        torrent_info=TorrentInfo(
            title="周杰伦 - 晴天 FLAC",
            category=MediaType.MUSIC.value,
        )
    )
    search_chain = Mock()
    search_chain.search_by_title.side_effect = [[context], []]
    download_chain = Mock()
    download_chain.batch_download.return_value = ([context], None)
    chain = SubscribeChain()
    chain.finish_subscribe_or_not = Mock()
    chain.check_and_handle_existing_media = Mock(return_value=(False, {}))
    chain.filter_torrents = Mock(side_effect=lambda **kwargs: kwargs["torrent_list"])
    subscribe_oper = Mock()
    subscribe_oper.get.return_value = subscribe
    chain.subscription_repository = subscribe_oper

    with patch.object(SubscribeChain, "_recognize_music_subscribe", return_value=target), \
            patch("app.chain._music.SearchChain", return_value=search_chain), \
            patch("app.chain._music.DownloadChain", return_value=download_chain):
        chain._search_music_subscribe(subscribe)

    search_chain.search_by_title.assert_any_call(
        title="周杰伦 晴天", sites=[], mtype=MediaType.MUSIC, rule_groups=[]
    )
    download_chain.batch_download.assert_called_once()
    matched_context = download_chain.batch_download.call_args.kwargs["contexts"][0]
    assert matched_context is not context
    assert matched_context.media_info is target
    assert isinstance(matched_context.meta_info, MetaMusic)
    assert matched_context.meta_info.org_string == "周杰伦 - 晴天 FLAC"
    assert matched_context.meta_info.audio_format == "FLAC"
    assert matched_context.meta_info.audio_lossless is True
    chain.finish_subscribe_or_not.assert_called_once()


def test_music_search_honours_cancel_before_external_work():
    """音乐搜索在安全边界收到取消后不得继续访问站点。"""
    subscribe = _subscribe()
    chain = SubscribeChain()
    execution_context = _execution_context(cancelled=lambda: True)

    with patch("app.chain._music.SearchChain") as search_chain, \
            pytest.raises(SubscriptionSearchCancelled):
        chain._search_music_subscribe(
            subscribe,
            execution_context=execution_context,
        )

    search_chain.assert_not_called()


def test_music_download_marks_shared_execution_context_before_side_effect():
    """音乐下载必须把取消和副作用边界传入统一下载治理。"""
    cancelled = [False]
    subscribe = _subscribe()
    target = _music_info()
    downloaded = Context(
        torrent_info=TorrentInfo(
            title="周杰伦 - 晴天 FLAC",
            category=MediaType.MUSIC.value,
        ),
        meta_info=MetaMusic.from_music_info(target),
        media_info=target,
    )
    execution_context = _execution_context(cancelled=lambda: cancelled[0])
    download_chain = Mock()

    def batch_download(**kwargs):
        """模拟下载器边界内开始提交后才收到取消。"""
        governance = kwargs["governance"]
        assert governance.cancelled() is False
        governance.mark_started()
        cancelled[0] = True
        return [downloaded], None

    download_chain.batch_download.side_effect = batch_download
    repository = Mock()
    repository.get.return_value = subscribe
    chain = SubscribeChain()
    chain.subscription_repository = repository
    chain.finish_subscribe_or_not = Mock()

    with patch("app.chain._music.DownloadChain", return_value=download_chain):
        chain._download_music_subscribe(
            subscribe,
            target,
            [downloaded],
            execution_context=execution_context,
        )

    assert execution_context.download_started is True
    chain.finish_subscribe_or_not.assert_called_once()


def test_music_download_rechecks_paused_state_before_submission():
    """候选准备后暂停的音乐订阅不得进入下载器。"""
    subscribe = _subscribe()
    paused = _subscribe(state="S")
    repository = Mock()
    repository.get.return_value = paused
    chain = SubscribeChain()
    chain.subscription_repository = repository

    with patch("app.chain._music.DownloadChain") as download_chain:
        chain._download_music_subscribe(
            subscribe,
            _music_info(),
            [Context()],
            execution_context=_execution_context(),
        )

    download_chain.assert_not_called()


def test_music_subscribe_filters_declared_bitrate_and_format():
    """音乐订阅应按规范化格式和最低码率过滤站点资源。"""
    subscribe = _subscribe(audio_format="MP3", min_bitrate=320000)
    contexts = [
        Context(torrent_info=TorrentInfo(
            title="周杰伦 - 晴天 MP3 192kbps", category=MediaType.MUSIC.value,
        )),
        Context(torrent_info=TorrentInfo(
            title="周杰伦 - 晴天 MP3 320kbps", category=MediaType.MUSIC.value,
        )),
    ]
    chain = SubscribeChain()
    chain.filter_torrents = Mock(side_effect=lambda **kwargs: kwargs["torrent_list"])

    matched = chain._filter_music_subscribe_contexts(subscribe, _music_info(), contexts)

    assert len(matched) == 1
    assert matched[0].meta_info.bitrate == 320000


def test_music_best_version_only_accepts_higher_audio_score():
    """音乐洗版只能接收高于当前版本的候选，并把音质分数写入下载优先级。"""
    subscribe = _subscribe(best_version=1, current_priority=90)
    contexts = [
        Context(torrent_info=TorrentInfo(
            title="周杰伦 - 晴天 FLAC 16bit 44.1kHz", category=MediaType.MUSIC.value,
        )),
        Context(torrent_info=TorrentInfo(
            title="周杰伦 - 晴天 FLAC 24bit 96kHz", category=MediaType.MUSIC.value,
        )),
    ]
    chain = SubscribeChain()
    chain.filter_torrents = Mock(side_effect=lambda **kwargs: kwargs["torrent_list"])

    matched = chain._filter_music_subscribe_contexts(subscribe, _music_info(), contexts)

    assert len(matched) == 1
    assert matched[0].meta_info.audio_quality_score == 96
    assert matched[0].torrent_info.pri_order == 96


def test_music_best_version_preserves_configured_format_priority():
    """音乐洗版应优先采用用户规则组给出的格式顺序，而非覆盖为自动音质分数。"""
    subscribe = _subscribe(best_version=1, current_priority=90)
    context = Context(torrent_info=TorrentInfo(
        title="周杰伦 - 晴天 MP3 320kbps",
        category=MediaType.MUSIC.value,
    ))
    chain = SubscribeChain()

    def apply_rule_priority(**kwargs):
        """模拟音乐格式规则组把当前候选排到最高优先级。"""
        kwargs["torrent_list"][0].pri_order = 100
        return kwargs["torrent_list"]

    chain.filter_torrents = Mock(side_effect=apply_rule_priority)

    matched = chain._filter_music_subscribe_contexts(
        subscribe,
        _music_info(),
        [context],
    )

    assert matched[0].meta_info.audio_quality_score == 80
    assert matched[0].torrent_info.pri_order == 100


def test_music_best_version_persists_downloaded_rule_priority():
    """音乐洗版成功后应按实际采用的规则优先级和音频参数更新当前版本。"""
    subscribe = _subscribe(best_version=1, current_priority=90)
    meta = MetaMusic(title="晴天")
    meta.apply_audio_quality("MP3 320kbps")
    downloaded = Context(
        torrent_info=TorrentInfo(
            title="周杰伦 - 晴天 MP3 320kbps",
            category=MediaType.MUSIC.value,
            pri_order=100,
        ),
        meta_info=meta,
    )
    download_chain = Mock()
    download_chain.batch_download.return_value = ([downloaded], None)
    subscribe_oper = Mock()
    updated = _subscribe(best_version=1, current_priority=100)
    subscribe_oper.get.return_value = subscribe
    subscribe_oper.update.return_value = updated
    chain = SubscribeChain()
    chain.finish_subscribe_or_not = Mock()
    _configure_subscription_write(chain, subscribe_oper)

    with patch("app.chain._music.DownloadChain", return_value=download_chain):
        chain._download_music_subscribe(subscribe, _music_info(), [downloaded])

    subscribe_oper.update.assert_called_once_with(
        subscribe.id,
        SubscriptionPatch({
            "current_priority": 100,
            "current_audio_format": "MP3",
            "current_bitrate": 320_000,
            "current_bit_depth": None,
            "current_sample_rate": None,
        }),
    )
    assert subscribe.current_priority == 90
    chain.finish_subscribe_or_not.assert_called_once()
    persisted = chain.finish_subscribe_or_not.call_args.kwargs["subscribe"]
    assert persisted.current_priority == updated.current_priority
    chain.finish_subscribe_or_not.assert_called_once()


def test_music_filter_keeps_cached_torrent_priority_isolated():
    """音乐订阅规则写入优先级时不得污染供其它订阅复用的 RSS 缓存。"""
    subscribe = _subscribe(best_version=1, current_priority=90)
    source_torrent = TorrentInfo(
        title="周杰伦 - 晴天 MP3 320kbps",
        category=MediaType.MUSIC.value,
        pri_order=0,
    )
    source_context = Context(torrent_info=source_torrent)
    chain = SubscribeChain()

    def apply_rule_priority(**kwargs):
        """模拟过滤模块在传入对象上写入规则优先级。"""
        kwargs["torrent_list"][0].pri_order = 100
        return kwargs["torrent_list"]

    chain.filter_torrents = Mock(side_effect=apply_rule_priority)

    matched = chain._filter_music_subscribe_contexts(
        subscribe,
        _music_info(),
        [source_context],
    )

    assert len(matched) == 1
    assert matched[0].torrent_info is not source_torrent
    assert matched[0].torrent_info.pri_order == 100
    assert source_torrent.pri_order == 0


def test_music_subscribe_matches_artist_from_resource_description():
    """站点主标题只有曲名时，应允许使用副标题中的艺术家完成目标复核。"""
    subscribe = _subscribe()
    context = Context(torrent_info=TorrentInfo(
        title="晴天 FLAC",
        description="周杰伦 - 叶惠美 2003",
        category=MediaType.MUSIC.value,
    ))
    chain = SubscribeChain()
    chain.filter_torrents = Mock(side_effect=lambda **kwargs: kwargs["torrent_list"])

    matched = chain._filter_music_subscribe_contexts(
        subscribe,
        _music_info(),
        [context],
    )

    assert len(matched) == 1


def test_album_best_version_requires_confirmed_full_coverage():
    """最高优先级的非完整专辑不得写入洗版基线或完成订阅。"""
    subscribe = _subscribe(
        name="叶惠美",
        music_type=MUSIC_ENTITY_ALBUM,
        total_tracks=11,
        best_version=1,
        current_priority=90,
    )
    album = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        album="叶惠美",
        artists=["周杰伦"],
        total_tracks=11,
    )
    meta = MetaMusic.from_music_info(album)
    downloaded = Context(
        torrent_info=TorrentInfo(
            title="周杰伦 - 叶惠美 FLAC",
            category=MediaType.MUSIC.value,
            pri_order=100,
        ),
        meta_info=meta,
        media_info=album,
        confirmed_full_coverage=False,
    )
    download_chain = Mock()
    download_chain.batch_download.return_value = ([downloaded], None)
    subscribe_oper = Mock()
    subscribe_oper.get.return_value = subscribe
    chain = SubscribeChain()
    _configure_subscription_write(chain, subscribe_oper)

    with patch("app.chain._music.DownloadChain", return_value=download_chain), \
            patch.object(chain, "_SubscribeChain__finish_subscribe") as finish:
        chain._download_music_subscribe(subscribe, album, [downloaded])

    subscribe_oper.update.assert_not_called()
    assert subscribe.current_priority == 90
    finish.assert_not_called()


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
    chain.check_and_handle_existing_media = Mock(return_value=(False, {}))

    with patch.object(SubscribeChain, "_recognize_music_subscribe", return_value=_music_info()), \
            patch("app.chain._music.SearchChain", return_value=search_chain), \
            patch("app.chain._music.DownloadChain") as download_chain:
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

    chain = SubscribeChain()
    chain.check_and_handle_existing_media = Mock(return_value=(False, {}))

    with patch.object(SubscribeChain, "_recognize_music_subscribe", return_value=_music_info()), \
            patch("app.chain._music.SearchChain", return_value=search_chain), \
            patch("app.chain._music.DownloadChain") as download_chain:
        chain._search_music_subscribe(subscribe)

    download_chain.assert_not_called()


def test_music_subscribe_skips_search_when_target_is_already_in_library():
    """单曲或完整专辑已在媒体库时应直接完成查重处理，不得重复搜索和下载。"""
    subscribe = _subscribe()
    chain = SubscribeChain()
    chain.check_and_handle_existing_media = Mock(return_value=(True, {}))

    with patch.object(SubscribeChain, "_recognize_music_subscribe", return_value=_music_info()), \
            patch("app.chain._music.SearchChain") as search_chain, \
            patch("app.chain._music.DownloadChain") as download_chain:
        chain._search_music_subscribe(subscribe)

    chain.check_and_handle_existing_media.assert_called_once()
    search_chain.assert_not_called()
    download_chain.assert_not_called()


def test_music_rss_match_reuses_cached_context_without_second_site_search():
    """订阅刷新应直接消费 RSS 音乐上下文，不得为每条音乐订阅再次调用站点搜索。"""
    subscribe = _subscribe()
    target = _music_info()
    source_context = Context(
        torrent_info=TorrentInfo(
            title="周杰伦 - 晴天 FLAC",
            category=MediaType.MUSIC.value,
            site=1,
            site_name="MusicSite",
        )
    )
    subscribe_oper = Mock()
    subscribe_oper.list.return_value = [subscribe]
    subscribe_oper.get.return_value = subscribe
    download_chain = Mock()
    download_chain.batch_download.side_effect = lambda **kwargs: (kwargs["contexts"], None)
    chain = SubscribeChain()
    chain.check_and_handle_existing_media = Mock(return_value=(False, {}))
    chain.get_sub_sites = Mock(return_value=[])
    chain.get_params = Mock(return_value={})
    chain.filter_torrents = Mock(side_effect=lambda **kwargs: kwargs["torrent_list"])
    chain.finish_subscribe_or_not = Mock()
    chain.subscription_repository = subscribe_oper

    torrent_helper = Mock()
    torrent_helper.filter_torrent.return_value = True
    with patch.object(SubscribeChain, "_recognize_music_subscribe", return_value=target), \
            patch("app.chain._music.TorrentHelper", return_value=torrent_helper), \
            patch("app.chain._music.DownloadChain", return_value=download_chain), \
            patch.object(chain, "_music_search_chain") as search_chain, \
            patch.object(chain, "_music_media_chain") as media_chain:
        chain.match({"music.example": [source_context]})

    search_chain.assert_not_called()
    media_chain.assert_not_called()
    download_chain.batch_download.assert_called_once()
    matched_context = download_chain.batch_download.call_args.kwargs["contexts"][0]
    assert matched_context is not source_context
    assert matched_context.media_info is target
    assert matched_context.meta_info.org_string == "周杰伦 - 晴天 FLAC"
    chain.finish_subscribe_or_not.assert_called_once()


def test_album_subscription_uses_persisted_snapshot_when_remote_detail_is_unavailable():
    """远端详情短暂失败时应从订阅快照恢复专辑语义，不能按标题猜成第一首单曲。"""
    subscribe = _subscribe(
        name="叶惠美",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        total_tracks=11,
        description="周杰伦 · Album · 2003-07-31",
        media_category=" 音乐 / 华语 // 专辑 ",
    )
    media_chain = _music_recognition_chain()

    with patch("app.chain._music.MediaChain", return_value=media_chain):
        restored = SubscribeChain._recognize_music_subscribe(subscribe)

    assert restored.music_type == MUSIC_ENTITY_ALBUM
    assert restored.album == "叶惠美"
    assert restored.artists == ["周杰伦"]
    assert restored.total_tracks == 11
    finalize_call = media_chain._finalize_recognition_result.call_args
    assert finalize_call.args == (restored,)
    assert finalize_call.kwargs["effective_override"] == ClassificationSelection(
        category_path=["音乐", "华语", "专辑"],
        source="subscription",
    )


def test_music_subscription_remote_result_uses_manual_category_override() -> None:
    """远端音乐详情应在订阅边界应用去空段后的手工分类覆盖。"""
    subscribe = _subscribe(media_category=" 音乐 / 华语 // 流行 ")
    remote = _music_info()
    media_chain = _music_recognition_chain(remote)

    with patch("app.chain._music.MediaChain", return_value=media_chain):
        restored = SubscribeChain._recognize_music_subscribe(subscribe)

    assert restored is remote
    finalize_call = media_chain._finalize_recognition_result.call_args
    assert finalize_call.args == (remote,)
    assert finalize_call.kwargs["effective_override"] == ClassificationSelection(
        category_path=["音乐", "华语", "流行"],
        source="subscription",
    )


def test_music_subscription_without_manual_category_keeps_automatic_classification() -> None:
    """空白手工分类应传入 None，使最终分类服务继续执行自动规则。"""
    subscribe = _subscribe(media_category=" / / ")
    remote = _music_info()
    automatic = _music_info()
    automatic.set_library_category("音乐/自动")
    media_chain = _music_recognition_chain(remote)
    media_chain._finalize_recognition_result.side_effect = None
    media_chain._finalize_recognition_result.return_value = automatic

    with patch("app.chain._music.MediaChain", return_value=media_chain):
        restored = SubscribeChain._recognize_music_subscribe(subscribe)

    assert restored is automatic
    assert restored.library_category == "音乐/自动"
    finalize_call = media_chain._finalize_recognition_result.call_args
    assert finalize_call.args == (remote,)
    assert finalize_call.kwargs["effective_override"] is None


def test_legacy_music_identity_failure_does_not_guess_entity_from_title():
    """旧订阅有标准 ID 却无实体类型时，识别失败后应保留订阅而不是误选标题搜索首项。"""
    subscribe = _subscribe(music_type=None)
    media_chain = _music_recognition_chain()

    with patch("app.chain._music.MediaChain", return_value=media_chain):
        restored = SubscribeChain._recognize_music_subscribe(subscribe)

    assert restored is None
    media_chain._finalize_recognition_result.assert_called_once_with(
        None,
        effective_override=None,
    )


def test_album_subscription_without_remote_id_uses_persisted_entity_snapshot():
    """专辑快照缺少远端 ID 时也不得退化为单曲识别。"""
    subscribe = _subscribe(
        name="叶惠美",
        media_source=None,
        media_id=None,
        music_type=MUSIC_ENTITY_ALBUM,
        total_tracks=11,
    )

    media_chain = _music_recognition_chain()
    with patch("app.chain._music.MediaChain", return_value=media_chain):
        restored = SubscribeChain._recognize_music_subscribe(subscribe)

    assert restored.music_type == MUSIC_ENTITY_ALBUM
    assert restored.total_tracks == 11
    media_chain.recognize_media.assert_not_called()
    media_chain._finalize_recognition_result.assert_called_once_with(
        restored,
        effective_override=None,
    )


def test_legacy_music_without_identity_uses_recording_recognition_boundary():
    """旧订阅缺少标准身份时只能恢复为单曲，不能消费全局搜索中的专辑或艺术家候选。"""
    subscribe = _subscribe(
        media_source=None,
        media_id=None,
        music_type=None,
    )
    recording = _music_info()
    media_chain = _music_recognition_chain(recording)

    with patch("app.chain._music.MediaChain", return_value=media_chain):
        restored = SubscribeChain._recognize_music_subscribe(subscribe)

    assert restored is recording
    media_chain.recognize_media.assert_called_once()
    call = media_chain.recognize_media.call_args
    assert isinstance(call.kwargs["meta"], MetaMusic)
    assert call.kwargs["mtype"] == MediaType.MUSIC


def test_legacy_music_subscription_rejects_artist_recognition_result():
    """旧订阅缺少实体类型时不得把艺术家识别结果迁移成可下载订阅。"""
    subscribe = _subscribe(music_type=None)
    artist = MusicInfo(
        media_source="musicbrainz",
        media_id="artist-1",
        music_type=MUSIC_ENTITY_ARTIST,
        title="周杰伦",
        artists=["周杰伦"],
    )
    media_chain = _music_recognition_chain(artist)

    with patch("app.chain._music.MediaChain", return_value=media_chain):
        restored = SubscribeChain._recognize_music_subscribe(subscribe)

    assert restored is None
    media_chain._finalize_recognition_result.assert_called_once_with(
        None,
        effective_override=None,
    )


def test_album_subscription_preserves_track_count_snapshot_when_remote_omits_it():
    """专辑远端详情暂缺曲目数时应复用持久化快照，且不得修改共享识别对象。"""
    subscribe = _subscribe(
        name="叶惠美",
        music_type=MUSIC_ENTITY_ALBUM,
        total_tracks=11,
        media_id="release-group-1",
    )
    remote = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        album="叶惠美",
        artists=["周杰伦"],
        total_tracks=None,
    )
    media_chain = _music_recognition_chain(remote)

    with patch("app.chain._music.MediaChain", return_value=media_chain):
        restored = SubscribeChain._recognize_music_subscribe(subscribe)

    assert restored is not remote
    assert restored.total_tracks == 11
    assert remote.total_tracks is None


def test_async_music_subscription_remote_result_uses_manual_category_override() -> None:
    """异步远端识别结果应使用与同步路径一致的订阅分类覆盖。"""
    subscribe = _subscribe(media_category=" 音乐 / 欧美 / 摇滚 ")
    remote = _music_info()
    media_chain = _music_recognition_chain(remote)

    with patch("app.chain._music.MediaChain", return_value=media_chain):
        restored = asyncio.run(
            SubscribeChain._async_recognize_music_subscribe(subscribe)
        )

    assert restored is remote
    media_chain.async_recognize_media.assert_awaited_once()
    finalize_call = media_chain._finalize_recognition_result.call_args
    assert finalize_call.args == (remote,)
    assert finalize_call.kwargs["effective_override"] == ClassificationSelection(
        category_path=["音乐", "欧美", "摇滚"],
        source="subscription",
    )


def test_async_music_subscription_fallback_snapshot_is_finalized() -> None:
    """异步远端失败后的持久化快照也必须经过同一最终分类入口。"""
    subscribe = _subscribe(
        name="叶惠美",
        music_type=MUSIC_ENTITY_ALBUM,
        total_tracks=11,
        media_category=None,
    )
    media_chain = _music_recognition_chain()

    with patch("app.chain._music.MediaChain", return_value=media_chain):
        restored = asyncio.run(
            SubscribeChain._async_recognize_music_subscribe(subscribe)
        )

    assert restored.music_type == MUSIC_ENTITY_ALBUM
    assert restored.total_tracks == 11
    media_chain._finalize_recognition_result.assert_called_once_with(
        restored,
        effective_override=None,
    )


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
        media_source="musicbrainz",
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
    updated = _subscribe(total_tracks=None)
    subscribe_oper.update.return_value = updated

    chain = SubscribeChain()
    _configure_subscription_write(chain, subscribe_oper)
    result = chain._sync_music_subscribe_target(subscribe, _music_info())

    subscribe_oper.update.assert_called_once_with(
        subscribe.id,
        SubscriptionPatch({"total_tracks": None}),
    )
    assert result.total_tracks == updated.total_tracks
    assert subscribe.total_tracks == 11


def test_album_target_sync_does_not_clear_stable_track_count():
    """专辑详情缺少曲目数时，同步逻辑不得清空已确认的完整性快照。"""
    subscribe = _subscribe(
        name="叶惠美",
        music_type=MUSIC_ENTITY_ALBUM,
        total_tracks=11,
    )
    album = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        album="叶惠美",
        total_tracks=None,
    )
    subscribe_oper = Mock()

    chain = SubscribeChain()
    chain.subscription_repository = subscribe_oper
    chain._sync_music_subscribe_target(subscribe, album)

    subscribe_oper.update.assert_not_called()
    assert subscribe.total_tracks == 11


def test_prepare_music_subscription_rejects_album_without_track_count():
    """搜索入口必须在同步和查库前拒绝无法验证完整覆盖的专辑。"""
    subscribe = _subscribe(
        name="未知专辑",
        music_type=None,
        total_tracks=None,
    )
    album = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-unknown",
        music_type=MUSIC_ENTITY_ALBUM,
        title="未知专辑",
        album="未知专辑",
        total_tracks=None,
    )
    chain = SubscribeChain()
    chain.check_and_handle_existing_media = Mock()

    with patch.object(SubscribeChain, "_recognize_music_subscribe", return_value=album), \
            patch.object(SubscribeChain, "_sync_music_subscribe_target") as sync_target:
        prepared = chain._prepare_music_subscribe(subscribe)

    assert prepared is None
    sync_target.assert_not_called()
    chain.check_and_handle_existing_media.assert_not_called()


def test_subscribe_add_music_uses_explicit_entity_recognize():
    """带稳定身份的音乐订阅应按来源、ID 和实体直查，不再按标题换目标。"""
    target = _music_info()
    media_chain = Mock()
    media_chain.recognize_media = Mock(return_value=target)
    # 落库入口已迁到 application/subscription/write.py，链路层能截到的接缝是 add_subscribe，
    # 它收到的正是链路交给写入路径的那份字段
    add_subscribe = Mock(return_value=(1, ""))

    with patch("app.chain.subscribe.create.MediaChain", return_value=media_chain), \
            patch("app.chain.subscribe.create.add_subscribe", add_subscribe), \
            patch("app.startup.composition.chain.MoviePilotServerHelper"), \
            patch.object(SubscribeChain, "_SubscribeChain__post_subscribe_added"):
        sid, err_msg = SubscribeChain().add(
            title="周杰伦 - 晴天",
            year="2003",
            mtype=MediaType.MUSIC,
            media_source="musicbrainz",
            media_id="recording-1",
            music_type=MUSIC_ENTITY_RECORDING,
            message=False,
        )

    assert sid == 1
    assert err_msg == ""
    media_chain.recognize_media.assert_called_once()
    routed_meta = media_chain.recognize_media.call_args.kwargs["meta"]
    assert isinstance(routed_meta, MetaMusic)
    assert routed_meta.media_id == "recording-1"
    assert media_chain.recognize_media.call_args.kwargs["media_source"] == MediaSource.MusicBrainz
    assert media_chain.recognize_media.call_args.kwargs["music_type"] == MUSIC_ENTITY_RECORDING
    media_chain.recognize_by_meta.assert_not_called()


@pytest.mark.parametrize(
    ("media_source", "media_id", "title"),
    [
        ("theaudiodb", "2109619", "Parachutes"),
        ("doubanmusic", "1401853", "范特西"),
    ],
)
def test_subscribe_add_music_routes_new_album_sources(
        media_source: str,
        media_id: str,
        title: str,
):
    """新增音乐源的专辑订阅应保留来源、原生 ID 与实体类型。"""
    target = MusicInfo(
        media_source=media_source,
        media_id=media_id,
        music_type=MUSIC_ENTITY_ALBUM,
        title=title,
        album=title,
        total_tracks=10,
    )
    media_chain = Mock()
    media_chain.recognize_media = Mock(return_value=target)
    add_subscribe = Mock(return_value=(1, ""))

    with patch("app.chain.subscribe.create.MediaChain", return_value=media_chain), \
            patch("app.chain.subscribe.create.add_subscribe", add_subscribe), \
            patch("app.startup.composition.chain.MoviePilotServerHelper"), \
            patch.object(SubscribeChain, "_SubscribeChain__post_subscribe_added"):
        sid, err_msg = SubscribeChain().add(
            title=title,
            year="2000",
            mtype=MediaType.MUSIC,
            media_source=media_source,
            media_id=media_id,
            music_type=MUSIC_ENTITY_ALBUM,
            message=False,
        )

    assert sid == 1
    assert err_msg == ""
    media_chain.recognize_media.assert_called_once()
    assert media_chain.recognize_media.call_args.kwargs["media_source"] == MediaSource(media_source)
    assert media_chain.recognize_media.call_args.kwargs["media_id"] == media_id
    assert media_chain.recognize_media.call_args.kwargs["music_type"] == MUSIC_ENTITY_ALBUM
    assert add_subscribe.call_args.kwargs["media_source"] == MediaSource(media_source)
    assert add_subscribe.call_args.kwargs["media_id"] == media_id
    media_chain.recognize_by_meta.assert_not_called()


def test_subscribe_add_rejects_music_entity_mismatch_before_database_write():
    """请求专辑却识别为单曲时必须中止，不能创建完成语义错误的订阅。"""
    media_chain = Mock()
    media_chain.recognize_media.return_value = _music_info()
    add_subscribe = Mock()

    with patch("app.chain.subscribe.create.MediaChain", return_value=media_chain), \
            patch("app.chain.subscribe.create.add_subscribe", add_subscribe):
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
    media_chain.recognize_by_meta.assert_not_called()
    add_subscribe.assert_not_called()


def test_subscribe_add_music_fails_fast_on_offline_fallback():
    """统一识别返回离线兜底（无远端 source）时订阅应直接失败，不写入数据库。"""
    offline = MusicInfo(title="未知曲目", artists=["未知艺术家"])
    media_chain = Mock()
    media_chain.recognize_by_meta = Mock(return_value=offline)
    add_subscribe = Mock(return_value=(1, ""))

    with patch("app.chain.subscribe.create.MediaChain", return_value=media_chain), \
            patch("app.chain.subscribe.create.add_subscribe", add_subscribe):
        sid, err_msg = SubscribeChain().add(
            title="未知曲目",
            year=None,
            mtype=MediaType.MUSIC,
            message=False,
        )

    assert sid is None
    assert err_msg == "未识别到媒体信息"
    add_subscribe.assert_not_called()


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
    subscribe_oper.history_exists.return_value = False
    system_config = Mock()
    system_config.get.return_value = ["follow-user"]

    chain = SubscribeChain()
    chain.subscription_repository = subscribe_oper
    with patch("app.chain.subscribe.query.get_configured_system_config", return_value=system_config), \
            patch(
                "app.startup.composition.chain.MoviePilotServerHelper.get_subscribe_shares",
                return_value=[share],
            ), \
            patch("app.chain.subscribe.query.MetaInfo") as video_meta, \
            patch.object(SubscribeChain, "add", return_value=(1, "")) as add:
        chain.follow(repository=subscribe_oper)

    video_meta.assert_not_called()
    identity = SubscriptionIdentity(
        media_source=MediaSource.MusicBrainz,
        media_id="release-group-1",
        type=MediaType.MUSIC.value,
        music_type=MUSIC_ENTITY_ALBUM,
    )
    subscribe_oper.exists.assert_called_once_with(identity)
    subscribe_oper.history_exists.assert_called_once_with(identity)
    assert add.call_args.kwargs["music_type"] == MUSIC_ENTITY_ALBUM
    assert add.call_args.kwargs["total_tracks"] == 11


def test_refresh_enables_music_entry_fetch_when_music_subscribe_exists():
    """存在音乐订阅时，订阅刷新应要求种子链额外抓取站点音乐专用入口。"""
    chain = SubscribeChain()
    subscribe_oper = Mock()
    # get_subscribed_sites 不带状态查询，has_music_subscribe 按可搜索状态查询
    subscribe_oper.list.side_effect = lambda state=None: [_subscribe(state="R")]
    chain.subscription_repository = subscribe_oper
    torrents_chain = Mock()
    torrents_chain.refresh.return_value = {}

    with patch("app.chain.subscribe.query.get_configured_system_config") as system_config, \
            patch("app.chain.subscribe.refresh.TorrentsChain", return_value=torrents_chain):
        system_config.return_value.get.return_value = []
        chain.refresh()

    assert torrents_chain.refresh.call_args.kwargs["include_music"] is True
