import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.subscription.contract import (
    AfterCommitEffect,
    AsyncAfterCommitEffect,
    SubscriptionIdentity,
    SubscriptionPatch,
)
from app.application.subscription.query import SubscriptionQueryService
from app.application.subscription.write import add_subscribe, async_add_subscribe
from app.db.models.subscribe import Subscribe
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.subscribehistory import SubscribeHistoryOper
from app.domain.context import MusicInfo
from app.schemas.types import MediaSource, MediaType


class _OperWriteAdapter:
    """在测试中显式模拟 DB adapter 的 DTO 解包边界。"""

    def __init__(self, repository: SubscribeOper) -> None:
        """保存待验证查重与建模行为的表级 Oper。"""
        self._repository = repository

    def add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: str | None = None,
        after_commit: AfterCommitEffect | None = None,
        notification=None,
    ) -> tuple[int, str]:
        """解包 typed DTO 后调用同步表级写入。"""
        return self._repository.add(
            identity.to_payload(),
            payload.to_payload(),
            username,
            after_commit,
        )

    async def async_add(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: str | None = None,
        after_commit: AsyncAfterCommitEffect | None = None,
        notification=None,
    ) -> tuple[int, str]:
        """解包 typed DTO 后调用异步表级写入。"""
        return await self._repository.async_add(
            identity.to_payload(),
            payload.to_payload(),
            username,
            after_commit,
        )


def _add(**kwargs):
    """
    经应用层写入路径新增订阅。

    媒体翻译住在 app/application/subscription/write.py，查重与落库仍在 SubscribeOper——本文件
    钉的是查重语义（谁被查、查几次、带哪些身份字段），所以从翻译入口进、把不带真会话
    的 Oper 注进去，两层的契约一次跑通。
    """
    return add_subscribe(
        subscribe_oper=_OperWriteAdapter(SubscribeOper(db=MagicMock())),
        **kwargs,
    )


async def _async_add(**kwargs):
    """异步写入路径，与 _add 共用注入方式。"""
    session = MagicMock()
    session.flush = AsyncMock()
    return await async_add_subscribe(
        subscribe_oper=_OperWriteAdapter(SubscribeOper(db=session)),
        **kwargs,
    )


def _media(episode_group):
    """构造订阅新增路径所需的稳定 MediaInfo 契约替身。"""
    return SimpleNamespace(
        title="测试剧",
        year="2026",
        type=MediaType.TV,
        media_source=MediaSource.TMDB,
        media_id="987654321",
        episode_group=episode_group,
        vote_average=8.0,
        overview="测试简介",
        get_poster_image=lambda: None,
        get_backdrop_image=lambda: None,
    )


def test_add_history_converts_boolean_integer_flags(monkeypatch):
    """
    写入订阅历史前应把布尔开关转为整型，兼容 PostgreSQL 的严格类型检查。
    """
    captured = {}

    def fake_stage_create(_oper, model):
        """
        截获待写入模型，避免测试依赖具体数据库方言的类型宽松行为。
        """
        captured.update(
            {
                "id": model.id,
                "best_version": model.best_version,
                "best_version_full": model.best_version_full,
                "search_imdbid": model.search_imdbid,
            }
        )
        return model

    monkeypatch.setattr(SubscribeHistoryOper, "_stage_create", fake_stage_create)

    SubscribeHistoryOper().add(
        {
            "id": 100,
            "name": "Test Movie",
            "type": "电影",
            "best_version": False,
            "best_version_full": True,
            "search_imdbid": False,
            "unknown_field": True,
        }
    )

    assert captured == {
        "id": None,
        "best_version": 0,
        "best_version_full": 1,
        "search_imdbid": 0,
    }


@pytest.mark.parametrize("episode_group", [None, "eg-1"])
def test_add_scopes_duplicate_lookup_by_episode_group(episode_group):
    """同步新增前后都必须按剧集组查询，主季和自定义组不能互相去重。"""
    persisted = SimpleNamespace(id=88)
    created = SimpleNamespace(create=MagicMock())

    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.exists.side_effect = [None, persisted]
        subscribe_model.return_value = created

        sid, message = _add(
            mediainfo=_media(episode_group),
            season=1,
        )

    assert (sid, message) == (88, "新增订阅成功")
    assert subscribe_model.exists.call_count == 2
    assert all(call.kwargs["episode_group"] == episode_group for call in subscribe_model.exists.call_args_list)


# 媒体身份的三种残缺形态。守卫写的是 ``not media_source or not media_id``——只测「两者都空」
# 时 ``or`` 与 ``and`` 表现一致，必须把「只缺一半」的两种也测到，否则守卫被改宽也没人知道。
# 注意：真实的 resolve_media_identity 只会返回「两个都有」或「两个都空」，构造不出半残身份，
# 所以这里必须替换掉它才能把守卫本身的契约钉住。
_INCOMPLETE_IDENTITIES = [
    pytest.param((None, "987654321"), id="缺来源"),
    pytest.param((MediaSource.TMDB, None), id="缺原生ID"),
    pytest.param((None, None), id="两者皆缺"),
]


@pytest.mark.parametrize("identity", _INCOMPLETE_IDENTITIES)
def test_add_rejects_incomplete_media_identity(identity):
    """
    媒体身份只要缺一半就必须拒绝新增，且不得落库。

    身份不全的订阅写进去就是一条永远匹配不上资源的僵尸订阅，后续按身份去重也会失效。
    """
    with (
        patch("app.application.subscription.write.resolve_media_identity", return_value=identity),
        patch("app.db.oper.subscribe.Subscribe") as subscribe_model,
    ):
        result = _add(mediainfo=_media(None), season=1)

    assert result == (0, "未识别到媒体信息，请检查媒体来源和媒体 ID 后重试")
    # 守卫必须在查询与建模之前短路，而不是先写进去再补救
    subscribe_model.exists.assert_not_called()
    subscribe_model.assert_not_called()


@pytest.mark.parametrize("identity", _INCOMPLETE_IDENTITIES)
def test_async_add_rejects_incomplete_media_identity(identity):
    """异步新增与同步路径共用同一道身份守卫，两条链路不能一宽一严。"""
    with (
        patch("app.application.subscription.write.resolve_media_identity", return_value=identity),
        patch("app.db.oper.subscribe.Subscribe") as subscribe_model,
    ):
        subscribe_model.async_exists = AsyncMock()

        result = asyncio.run(_async_add(mediainfo=_media(None), season=1))

    assert result == (0, "未识别到媒体信息，请检查媒体来源和媒体 ID 后重试")
    subscribe_model.async_exists.assert_not_awaited()
    subscribe_model.assert_not_called()


def test_add_reports_failure_when_the_new_subscribe_cannot_be_read_back():
    """
    创建后回查落空必须如实报「新增订阅失败」，不能把落空当成功返回。

    回查落空意味着写入实际没生效（唯一约束冲突、事务回滚等）；此时若返回成功，
    调用方会继续按订阅已建立往下走，用户看到「订阅成功」却永远等不到资源。
    """
    created = SimpleNamespace(create=MagicMock())
    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.exists.side_effect = [None, None]
        subscribe_model.return_value = created

        result = _add(mediainfo=_media(None), season=1)

    assert result == (0, "新增订阅失败")


def test_async_add_reports_failure_when_the_new_subscribe_cannot_be_read_back():
    """异步新增的回查落空路径与同步一致。"""
    created = SimpleNamespace(async_create=AsyncMock())
    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.async_exists = AsyncMock(side_effect=[None, None])
        subscribe_model.return_value = created

        result = asyncio.run(_async_add(mediainfo=_media(None), season=1))

    assert result == (0, "新增订阅失败")


def test_add_reports_existing_subscription_without_creating():
    """
    首次查询即命中时返回既有订阅，不再建第二条。

    重复建订阅会让同一部剧被两条订阅并行搜索、重复下载。
    """
    existing = SimpleNamespace(id=77)
    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.exists.return_value = existing

        result = _add(mediainfo=_media(None), season=1)

    assert result == (77, "订阅已存在")
    assert subscribe_model.exists.call_count == 1
    subscribe_model.assert_not_called()


def test_async_add_reports_existing_subscription_without_creating():
    """异步新增命中既有订阅时同样不建第二条。"""
    existing = SimpleNamespace(id=78)
    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.async_exists = AsyncMock(return_value=existing)

        result = asyncio.run(_async_add(mediainfo=_media(None), season=1))

    assert result == (78, "订阅已存在")
    assert subscribe_model.async_exists.await_count == 1
    subscribe_model.assert_not_called()


def test_music_subscribe_persists_release_cover_as_poster_and_backdrop():
    """音乐订阅应把 MusicBrainz 发行封面写入订阅海报和背景字段。"""
    persisted = SimpleNamespace(id=92)
    created = SimpleNamespace(create=MagicMock())
    media = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="977e6978-139d-425c-bb98-6b0c62d1e45e",
        title="晴天",
        cover_url="https://coverartarchive.org/release-group/example/front-500",
    )

    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.exists.side_effect = [None, persisted]
        subscribe_model.return_value = created

        sid, _ = _add(mediainfo=media, season=None)

    assert sid == 92
    payload = subscribe_model.call_args.kwargs
    assert payload["poster"] == media.cover_url
    assert payload["backdrop"] == media.cover_url


def test_music_subscribe_persists_numeric_year_as_string():
    """音乐识别链路的年份可能是数字，写库前必须转字符串避免 PostgreSQL 类型错误。"""
    persisted = SimpleNamespace(id=93)
    created = SimpleNamespace(create=MagicMock())
    media = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="2af54891-e954-40e8-8b90-b7e98c740f21",
        title="心愿",
        year=2025,
        cover_url="https://coverartarchive.org/release/example/front-500",
    )

    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.exists.side_effect = [None, persisted]
        subscribe_model.return_value = created

        sid, _ = _add(mediainfo=media, season=None)

    assert sid == 93
    payload = subscribe_model.call_args.kwargs
    assert payload["year"] == "2025"


def test_music_album_subscription_persists_entity_and_track_count():
    """专辑订阅必须保存实体类型和总曲目数，供搜索校验与完成判定复用。"""
    persisted = SimpleNamespace(id=94)
    created = SimpleNamespace(create=MagicMock())
    media = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="release-group-1",
        music_type="album",
        title="叶惠美",
        album="叶惠美",
        total_tracks=11,
    )

    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.exists.side_effect = [None, persisted]
        subscribe_model.return_value = created

        sid, _ = _add(mediainfo=media, season=None)

    assert sid == 94
    payload = subscribe_model.call_args.kwargs
    assert payload["music_type"] == "album"
    assert payload["total_tracks"] == 11


def test_music_recording_subscription_drops_album_track_count_and_scopes_identity():
    """单曲只持久化实体类型，重复查询也必须携带实体，不能与专辑身份串用。"""
    persisted = SimpleNamespace(id=95)
    created = SimpleNamespace(create=MagicMock())
    media = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="recording-1",
        music_type="recording",
        title="晴天",
        album="叶惠美",
        total_tracks=11,
    )

    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.exists.side_effect = [None, persisted]
        subscribe_model.return_value = created

        sid, _ = _add(mediainfo=media, season=None)

    assert sid == 95
    payload = subscribe_model.call_args.kwargs
    assert payload["music_type"] == "recording"
    assert payload["total_tracks"] is None
    assert all(call.kwargs["music_type"] == "recording" for call in subscribe_model.exists.call_args_list)


@pytest.mark.parametrize("episode_group", [None, "eg-1"])
def test_async_add_scopes_duplicate_lookup_by_episode_group(episode_group):
    """异步新增与同步路径使用相同的剧集组身份契约。"""
    persisted = SimpleNamespace(id=89)
    created = SimpleNamespace(async_create=AsyncMock())

    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.async_exists = AsyncMock(side_effect=[None, persisted])
        subscribe_model.return_value = created

        sid, message = asyncio.run(
            _async_add(
                mediainfo=_media(episode_group),
                season=1,
            )
        )

    assert (sid, message) == (89, "新增订阅成功")
    assert subscribe_model.async_exists.await_count == 2
    assert all(call.kwargs["episode_group"] == episode_group for call in subscribe_model.async_exists.await_args_list)


def test_owner_scoped_add_forwards_episode_group_sync_and_async():
    """按 owner 去重的同步与异步新增也必须使用同一剧集组身份。"""
    media = _media("eg-owner")
    sync_persisted = SimpleNamespace(id=90)
    sync_created = SimpleNamespace(create=MagicMock())
    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.exists_by_username.side_effect = [None, sync_persisted]
        subscribe_model.return_value = sync_created

        sid, _ = _add(
            mediainfo=media,
            season=1,
            username="alice",
            owner_scope=True,
        )

    assert sid == 90
    assert all(call.kwargs["episode_group"] == "eg-owner" for call in subscribe_model.exists_by_username.call_args_list)

    async_persisted = SimpleNamespace(id=91)
    async_created = SimpleNamespace(async_create=AsyncMock())
    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.async_exists_by_username = AsyncMock(side_effect=[None, async_persisted])
        subscribe_model.return_value = async_created

        sid, _ = asyncio.run(
            _async_add(
                mediainfo=media,
                season=1,
                username="alice",
                owner_scope=True,
            )
        )

    assert sid == 91
    assert all(
        call.kwargs["episode_group"] == "eg-owner" for call in subscribe_model.async_exists_by_username.await_args_list
    )


def test_exists_defaults_to_main_season_episode_group():
    """省略剧集组时按主季查询，显式剧集组按对应范围查询。"""
    oper = SubscribeOper(db=object())
    with patch("app.db.oper.subscribe.Subscribe") as subscribe_model:
        subscribe_model.exists.return_value = SimpleNamespace(id=1)

        assert oper.exists(media_source=MediaSource.TMDB, media_id="100", season=1) is True
        assert subscribe_model.exists.call_args.kwargs["episode_group"] is None

        assert (
            oper.exists(
                media_source=MediaSource.TMDB,
                media_id="100",
                season=1,
                episode_group="eg-1",
            )
            is True
        )
        assert subscribe_model.exists.call_args.kwargs["episode_group"] == "eg-1"

    history_oper = SubscribeHistoryOper(db=object())
    with patch("app.db.oper.subscribehistory.SubscribeHistory") as history_model:
        history_model.exists.return_value = SimpleNamespace(id=2)

        assert history_oper.exists(media_source=MediaSource.TMDB, media_id="100", season=1) is True
        assert history_model.exists.call_args.kwargs["episode_group"] is None

        assert (
            history_oper.exists(
                media_source=MediaSource.TMDB,
                media_id="100",
                season=1,
                episode_group="eg-1",
            )
            is True
        )
        assert history_model.exists.call_args.kwargs["episode_group"] == "eg-1"


def test_subscribe_exists_distinguishes_same_season_episode_groups(db):
    """同一媒体同一季的主季、自定义剧集组应分别命中各自订阅。"""
    db.watermark(Subscribe)
    media_id = str(-(900_000_000 + os.getpid()))
    rows = [
        Subscribe(
            name="主季订阅",
            type=MediaType.TV.value,
            state="N",
            media_source=MediaSource.TMDB.value,
            media_id=media_id,
            season=1,
            episode_group=None,
        ),
        Subscribe(
            name="剧集组订阅",
            type=MediaType.TV.value,
            state="N",
            media_source=MediaSource.TMDB.value,
            media_id=media_id,
            season=1,
            episode_group="eg-1",
        ),
    ]
    for row in rows:
        row.create(db.session)
    db.session.commit()

    main_season = Subscribe.exists(
        db.session,
        media_source=MediaSource.TMDB,
        media_id=media_id,
        season=1,
        episode_group=None,
    )
    main_name = main_season.name
    episode_group = Subscribe.exists(
        db.session,
        media_source=MediaSource.TMDB,
        media_id=media_id,
        season=1,
        episode_group="eg-1",
    )
    episode_group_name = episode_group.name

    assert main_name == "主季订阅"
    assert episode_group_name == "剧集组订阅"

    Subscribe.delete(db.session, rid=main_season.id)
    db.session.commit()
    assert (
        Subscribe.exists(
            db.session,
            media_source=MediaSource.TMDB,
            media_id=media_id,
            season=1,
        )
        is None
    )


def test_subscribe_exists_distinguishes_music_entities_with_same_source_id(db):
    """统一来源 ID 相同时，单曲与专辑仍是两条独立订阅身份。"""
    db.watermark(Subscribe)
    media_id = f"music-shared-{os.getpid()}"
    rows = [
        Subscribe(
            name="同名单曲",
            type=MediaType.MUSIC.value,
            state="N",
            media_source="musicbrainz",
            media_id=media_id,
            music_type="recording",
        ),
        Subscribe(
            name="同名专辑",
            type=MediaType.MUSIC.value,
            state="N",
            media_source="musicbrainz",
            media_id=media_id,
            music_type="album",
            total_tracks=10,
        ),
    ]
    for row in rows:
        row.create(db.session)
    db.session.commit()

    recording = Subscribe.exists(
        db.session,
        media_source="musicbrainz",
        media_id=media_id,
        music_type="recording",
    )
    album = Subscribe.exists(
        db.session,
        media_source="musicbrainz",
        media_id=media_id,
        music_type="album",
    )
    assert recording.name == "同名单曲"
    assert album.name == "同名专辑"


def test_subscribe_chain_exists_forwards_episode_group():
    """订阅前置存在性检查必须查询当前剧集组，不能退回主季范围。"""
    from app.chain.subscribe.facade import SubscribeChain

    media = _media("eg-1")
    meta = SimpleNamespace(begin_season=1)
    repository = MagicMock()
    repository.exists.return_value = True
    with patch.object(
        SubscribeChain,
        "_subscription_query",
        return_value=SubscriptionQueryService(repository),
    ):
        assert SubscribeChain.exists(media, meta) is True

    repository.exists.assert_called_once_with(
        SubscriptionIdentity(
            media_source=MediaSource.TMDB,
            media_id=media.media_id,
            music_type=None,
            season=1,
            episode_group="eg-1",
        )
    )
