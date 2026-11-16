"""
跨来源订阅的身份确认、创建幂等和页面状态回归，全程隔离外部媒体查询。
"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.endpoints.media import media_status
from app.api.endpoints.subscribe import subscribe_media_identity
from app.application.subscription.contract import SubscriptionIdentity, SubscriptionSnapshot
from app.application.subscription.lookup import SubscriptionIdentityLookup
from app.application.subscription.query import SubscriptionQueryService
from app.application.subscription.write import AsyncCreateSubscriptionBatchCommand
from app.chain.media import MediaChain
from app.chain.subscribe import identity as identity_owner
from app.chain.subscribe.context import _SubscribeCreateContext
from app.chain.subscribe.create import SubscribeCreateOwner
from app.chain.subscribe.metadata import SubscribeMetadataOwner
from app.db.adapters.subscription import SessionSubscriptionRepository, TransactionalSubscriptionRepository
from app.db.models.subscribe import Subscribe
from app.db.session import SessionFactory, async_session_scope
from app.db.uow import SqlAlchemyAsyncUnitOfWork
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.schemas.media import MediaStatusQuery
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def projection():
    """
    注入可观察的同步、异步投影，两个原生 ID 默认指向同一 TMDB 作品。
    """
    sync = Mock(return_value={"id": 259140})
    asynchronous = AsyncMock(return_value={"id": 259140})
    return sync, asynchronous, SubscriptionIdentityLookup(sync, asynchronous)


def _record(source=MediaSource.Bangumi, **changes):
    """
    构造带真实进度及保存配置的不可变订阅快照。
    """
    return replace(SubscriptionSnapshot(
        id=73, name="舊來源的標題", year="2026", type=MediaType.TV.value,
        media_source=source, media_id="637802", season=2, episode_group=None,
        username="alice", lack_episode=3, total_episode=12, save_path="/library/anime",
    ), **changes)


def _identity(source=MediaSource.TMDB, **changes):
    """
    构造完整查重身份，辅助 ID 数字相同不代表来源相同。
    """
    return replace(SubscriptionIdentity(
        media_source=source, media_id="259140", type=MediaType.TV.value, season=2,
    ), **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [MediaSource.Douban, MediaSource.Bangumi, MediaSource.AniList])
@pytest.mark.parametrize("reverse", [False, True])
async def test_lookup_confirms_identity_in_both_creation_orders(projection, source, reverse):
    """
    新旧条目的名称、年份不同仍按投影身份查重，覆盖各来源先后创建的两个方向。
    """
    sync, asynchronous, lookup = projection
    target = _identity(source if reverse else MediaSource.TMDB)
    record = _record(MediaSource.TMDB if reverse else source,
                     media_id="259140" if reverse else "637802", year="2024")
    assert lookup.find(target, [record], "alice") == [record]
    async_lookup = SubscriptionIdentityLookup(sync, asynchronous)
    assert await async_lookup.async_find(target, [record], "alice") == [record]
    assert record.lack_episode == 3 and record.save_path == "/library/anime"
    assert sync.call_count == asynchronous.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,username", [
    ({"type": MediaType.MOVIE.value}, "alice"),
    ({"season": 1}, "alice"),
    ({"episode_group": "alternate"}, "alice"),
    ({"username": "bob"}, "alice"),
    ({}, ""),
    ({"media_source": MediaSource.TMDB, "media_id": "637802"}, "alice"),
])
async def test_lookup_keeps_type_season_group_and_owner_boundaries(projection, changes, username):
    """
    不同季、类型、剧集组、用户和同来源不同作品不能通过跨源查重合并，且不发起投影。
    """
    sync, asynchronous, lookup = projection
    record = _record(**changes)
    assert lookup.find(_identity(), [record], username) == []
    assert await lookup.async_find(_identity(), [record], username) == []
    sync.assert_not_called()
    asynchronous.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {}, {"id": 0}, {"id": 999}])
@pytest.mark.parametrize("reverse", [False, True])
async def test_lookup_does_not_deduplicate_unconfirmed_or_conflicting_ids(projection, result, reverse):
    """
    相同数字原生 ID 或相同标题不能替代有效的共同身份，投影失败和冲突均保留独立订阅。
    """
    sync, asynchronous, lookup = projection
    sync.return_value = asynchronous.return_value = result
    record = _record(MediaSource.TMDB if reverse else MediaSource.Bangumi, media_id="259140")
    target = _identity(MediaSource.Bangumi if reverse else MediaSource.TMDB)
    assert lookup.find(target, [record], "alice") == []
    assert await SubscriptionIdentityLookup(sync, asynchronous).async_find(target, [record], "alice") == []


@pytest.mark.asyncio
async def test_lookup_projection_errors_are_cached_only_within_operation(projection):
    """
    外部转换故障保留原身份，同一次查询不反复请求，下一次操作允许重试。
    """
    sync, asynchronous, lookup = projection
    sync.side_effect = RuntimeError("offline")
    asynchronous.side_effect = RuntimeError("offline")
    assert lookup.find(_identity(), [_record()]) == []
    assert lookup.find(_identity(), [_record()]) == []
    assert sync.call_count == 1
    assert await SubscriptionIdentityLookup(sync, asynchronous).async_find(_identity(), [_record()]) == []
    asynchronous.side_effect = None
    assert await SubscriptionIdentityLookup(sync, asynchronous).async_find(_identity(), [_record()]) == [_record()]


@pytest.mark.asyncio
async def test_lookup_retains_projected_season_zero_and_reuses_batch_projection(projection):
    """
    来源投影得到的第零季不能退回第一季，批量重复查询共享当前操作缓存。
    """
    sync, asynchronous, lookup = projection
    asynchronous.return_value = {"id": 259140, "season": 0}
    target = _identity(MediaSource.AniList, season=None)
    record = _record(MediaSource.TMDB, media_id="259140", season=0)
    for _ in range(2):
        assert await lookup.async_find(target, [record, replace(record, id=74, season=1)]) == [record]
    asynchronous.assert_awaited_once()
    sync.assert_not_called()


def _context(source=MediaSource.TMDB):
    """
    构造已经完成识别与季集准备的真实创建上下文。
    """
    media = MediaInfo(media_source=source, media_id="259140", type=MediaType.TV, title="新來源標題")
    return _SubscribeCreateContext(
        title=media.title, year="2026", mtype=MediaType.TV, episode_group=None, season=2,
        channel=None, source=None, userid=None, username="alice", message=False, exist_ok=False,
        options={}, explicit_identity=True, media_source=source, media_id=media.media_id,
        requested_music_type=None, metainfo=MetaInfo(media.title), mediainfo=media,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_reason", ["music", "empty_user"])
async def test_creation_lookup_keeps_music_and_explicit_empty_user_contract(skip_reason):
    """
    音乐及显式跳过查重的空用户不读取影视候选，也不触发任何跨源转换。
    """
    context = _context()
    if skip_reason == "music":
        context.mediainfo.type = MediaType.MUSIC
    else:
        context.username = ""
    repository = Mock()
    assert identity_owner.find_existing_subscription(context, repository) is None
    assert await identity_owner.async_find_existing_subscription(context, repository) is None
    repository.list.assert_not_called()
    repository.async_list.assert_not_called()


def test_anilist_metadata_retry_uses_the_existing_refresh_cycle():
    """
    AniList 存量订阅也使用既有元数据巡检转换身份，成功时原位保留其订阅与进度。
    """
    record = _record(MediaSource.AniList)
    converted = MediaInfo(media_source=MediaSource.TMDB, media_id="259140", type=MediaType.TV,
                          title="统一后的作品", year="2024", seasons={2: [1, 2, 3]})
    media = SimpleNamespace(convert_media_identity=Mock(return_value={"id": 259140}),
                            recognize_media=Mock(return_value=converted))
    updated = replace(record, media_source=MediaSource.TMDB, media_id="259140")
    owner = SimpleNamespace(_SubscribeChain__is_tmdb_recognize_source=lambda: True,
                            _SubscribeChain__apply_subscribe_update=Mock(return_value=updated))
    result = SubscribeMetadataOwner._retry_subscribe_media_identity(owner, record, MetaInfo("作品 S02"), lambda: media)
    assert result is updated
    assert result.id == record.id and result.lack_episode == record.lack_episode
    assert owner._SubscribeChain__apply_subscribe_update.call_args.args[1]["media_source"] == MediaSource.TMDB


@pytest.mark.asyncio
async def test_creation_returns_existing_id_without_write_or_post_commit(projection, monkeypatch):
    """
    同步、异步创建确认重复后直接返回旧 ID，不重写进度，也不重发新增通知或下载副作用。
    """
    sync, asynchronous, _ = projection
    media = SimpleNamespace(convert_media_identity=sync, async_convert_media_identity=asynchronous)
    monkeypatch.setattr(identity_owner, "MediaChain", lambda: media)
    record = _record()
    repository = SimpleNamespace(list=Mock(return_value=[record]), async_list=AsyncMock(return_value=[record]))
    owner = SimpleNamespace(subscription_repository=repository)
    assert SubscribeCreateOwner._SubscribeChain__persist_subscribe_create(owner, _context()) == (73, "订阅已存在")
    assert await SubscribeCreateOwner._SubscribeChain__async_persist_subscribe_create(owner, _context()) == (73, "订阅已存在")
    assert record.lack_episode == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
async def test_creation_reuses_database_subscription_and_preserves_progress(db, projection, monkeypatch, async_mode):
    """
    真实数据库中旧来源订阅保持单行，名称、下载缺口和目标路径不会被新的来源覆写。
    """
    db.watermark(Subscribe)
    with SessionFactory() as session:
        record = Subscribe(**{key: value for key, value in _record().to_dict().items()
                              if key not in {"id", "activity_key"}})
        session.add(record)
        session.commit()
        subscribe_id = record.id
    sync, asynchronous, _ = projection
    monkeypatch.setattr(identity_owner, "MediaChain", lambda: SimpleNamespace(
        convert_media_identity=sync, async_convert_media_identity=asynchronous,
    ))
    repository = TransactionalSubscriptionRepository(sync_session=SessionFactory, async_session=async_session_scope)
    owner = SimpleNamespace(subscription_repository=repository)
    if async_mode:
        result = await SubscribeCreateOwner._SubscribeChain__async_persist_subscribe_create(owner, _context())
    else:
        result = SubscribeCreateOwner._SubscribeChain__persist_subscribe_create(owner, _context())
    assert result == (subscribe_id, "订阅已存在")
    actual = repository.get(subscribe_id)
    assert actual.name == "舊來源的標題" and actual.media_source == MediaSource.Bangumi
    assert actual.lack_episode == 3 and actual.save_path == "/library/anime"
    assert not [record for record in repository.list() if record.media_source == MediaSource.TMDB
                and record.media_id == "259140" and record.username == "alice"]


@pytest.mark.asyncio
async def test_batch_skips_duplicate_season_and_creates_remaining_season(db, projection, monkeypatch):
    """
    批量跨源订阅复用旧季 ID，同时正常提交其它季，重复季不产生新的持久副作用。
    """
    db.watermark(Subscribe)
    async with async_session_scope() as session:
        record = Subscribe(name="舊來源的標題", type=MediaType.TV.value, media_source=MediaSource.Bangumi,
                           media_id="637802", season=2, username="alice", lack_episode=3)
        session.add(record)
        await session.commit()
        subscribe_id = record.id
        sync, asynchronous, _ = projection
        monkeypatch.setattr(identity_owner, "MediaChain", lambda: SimpleNamespace(
            convert_media_identity=sync, async_convert_media_identity=asynchronous,
        ))
        contexts = [(_context(), None), (replace(_context(), season=3), None)]
        prepare = AsyncMock(side_effect=contexts)
        repository = SessionSubscriptionRepository(session)
        outbox = SimpleNamespace(stage=AsyncMock())
        command = AsyncCreateSubscriptionBatchCommand(repository, SqlAlchemyAsyncUnitOfWork(session), outbox)
        captured = []

        async def persist(requests):
            """
            保留真实批量事务，收集每季的新增或复用结果。
            """
            results = await command.execute(requests)
            captured.extend(results)
            return results

        owner = SimpleNamespace(
            subscription_repository=repository,
            _SubscribeChain__async_prepare_subscribe_create=prepare,
            _SubscribeChain__build_subscribe_notification=Mock(return_value=None),
            _SubscribeChain__subscribe_post_commit_context=SubscribeCreateOwner._SubscribeChain__subscribe_post_commit_context,
        )
        result = await SubscribeCreateOwner.async_add_batch(
            owner, title="新來源標題", year="2026", seasons=[2, 3],
            batch_writer=SimpleNamespace(async_add=persist), username="alice",
        )
        assert captured[0].subscribe_id == subscribe_id and not captured[0].created
        assert captured[1].created and result[0] == captured[1].subscribe_id
        assert outbox.stage.await_count == 2
        assert (await repository.async_get(subscribe_id)).lack_episode == 3


@pytest.mark.asyncio
async def test_detail_and_card_status_use_projection_before_title_fallback(projection):
    """
    详情与卡片在标题不同、未提供年份时也能显示已订阅，并保留当前用户及季模式。
    """
    _, _, lookup = projection
    record = _record()
    repository = SimpleNamespace(async_list_by_media_identity=AsyncMock(return_value=[]),
                                 async_list=AsyncMock(return_value=[record, replace(record, id=74, username="bob")]),
                                 async_list_by_title=AsyncMock(return_value=[]))
    query = SubscriptionQueryService(repository, async_repository=repository, identity_lookup=lookup)
    actor = SimpleNamespace(name="alice", is_superuser=False)
    result = await subscribe_media_identity("259140", MediaSource.TMDB, season=2, title="新來源標題",
                                            mtype=MediaType.TV, query=query, current_user=actor)
    assert result.id == 73
    statuses = await media_status([
        MediaStatusQuery(media_source=MediaSource.TMDB, media_id="259140", season=2, mtype=MediaType.TV),
    ], SimpleNamespace(find_item_id=AsyncMock(return_value=None)), query, actor)
    assert statuses[0].subscribed is True and statuses[0].seasons == {2: "normal"}
    repository.async_list_by_title.assert_not_awaited()
    repository.async_list.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
async def test_anilist_projection_uses_native_dispatch_and_season_aliases(async_mode):
    """
    AniList 内置转换通过模块分发读取多语言条目，保留续季和年份，并隔离对目标详情的修改。
    """
    source = {"id": 187999, "title": {"chinese": "青之蘆葦 第二季", "native": "アオアシ"},
              "format": "TV", "startDate": {"year": 2026}, "episodes": 12}
    target = {"id": 259140, "name": "青之芦苇"}
    owner = SimpleNamespace(run_module=Mock(return_value=source),
                            async_run_module=AsyncMock(return_value=source),
                            _match_tmdb_with_names=Mock(return_value=target),
                            _async_match_tmdb_with_names=AsyncMock(return_value=target))
    kwargs = dict(target_source=MediaSource.TMDB, media_source=MediaSource.AniList, media_id="187999")
    if async_mode:
        result = await MediaChain.async_convert_media_identity(owner, **kwargs)
        owner.async_run_module.assert_awaited_once_with("async_anilist_info", anilist_id=187999)
        matched = owner._async_match_tmdb_with_names.await_args.kwargs
    else:
        result = MediaChain.convert_media_identity(owner, **kwargs)
        owner.run_module.assert_called_once_with("anilist_info", anilist_id=187999)
        matched = owner._match_tmdb_with_names.call_args.kwargs
    assert result == {**target, "season": 2}
    assert "青之蘆葦" in matched["meta_names"]
    assert matched["year"] == "2026" and matched["season"] == 2
    assert "season" not in target
