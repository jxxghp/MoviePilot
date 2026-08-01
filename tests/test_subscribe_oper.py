import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.subscribe_oper import SubscribeOper
from app.schemas.types import MediaType


def _media(episode_group):
    """构造订阅新增路径所需的稳定 MediaInfo 契约替身。"""
    return SimpleNamespace(
        title="测试剧",
        year="2026",
        type=MediaType.TV,
        source="themoviedb",
        media_source="themoviedb",
        media_id="987654321",
        mediaid="tmdb:987654321",
        tmdb_id=987654321,
        imdb_id=None,
        tvdb_id=None,
        douban_id=None,
        bangumi_id=None,
        anilist_id=None,
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

    def fake_create(self, _db):
        """
        截获待写入模型，避免测试依赖具体数据库方言的类型宽松行为。
        """
        captured.update({
            "id": self.id,
            "best_version": self.best_version,
            "best_version_full": self.best_version_full,
            "search_imdbid": self.search_imdbid,
        })

    monkeypatch.setattr(SubscribeHistory, "create", fake_create)

    SubscribeOper().add_history(
        id=100,
        name="Test Movie",
        type="电影",
        best_version=False,
        best_version_full=True,
        search_imdbid=False,
        unknown_field=True,
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

    with patch("app.db.subscribe_oper.Subscribe") as subscribe_model:
        subscribe_model.exists.side_effect = [None, persisted]
        subscribe_model.return_value = created

        sid, message = SubscribeOper(db=object()).add(
            mediainfo=_media(episode_group),
            season=1,
        )

    assert (sid, message) == (88, "新增订阅成功")
    assert subscribe_model.exists.call_count == 2
    assert all(
        call.kwargs["episode_group"] == episode_group
        for call in subscribe_model.exists.call_args_list
    )
    created.create.assert_called_once()


@pytest.mark.parametrize("episode_group", [None, "eg-1"])
def test_async_add_scopes_duplicate_lookup_by_episode_group(episode_group):
    """异步新增与同步路径使用相同的剧集组身份契约。"""
    persisted = SimpleNamespace(id=89)
    created = SimpleNamespace(async_create=AsyncMock())

    with patch("app.db.subscribe_oper.Subscribe") as subscribe_model:
        subscribe_model.async_exists = AsyncMock(side_effect=[None, persisted])
        subscribe_model.return_value = created

        sid, message = asyncio.run(SubscribeOper(db=object()).async_add(
            mediainfo=_media(episode_group),
            season=1,
        ))

    assert (sid, message) == (89, "新增订阅成功")
    assert subscribe_model.async_exists.await_count == 2
    assert all(
        call.kwargs["episode_group"] == episode_group
        for call in subscribe_model.async_exists.await_args_list
    )
    created.async_create.assert_awaited_once()


def test_owner_scoped_add_forwards_episode_group_sync_and_async():
    """按 owner 去重的同步与异步新增也必须使用同一剧集组身份。"""
    media = _media("eg-owner")
    sync_persisted = SimpleNamespace(id=90)
    sync_created = SimpleNamespace(create=MagicMock())
    with patch("app.db.subscribe_oper.Subscribe") as subscribe_model:
        subscribe_model.exists_by_username.side_effect = [None, sync_persisted]
        subscribe_model.return_value = sync_created

        sid, _ = SubscribeOper(db=object()).add(
            mediainfo=media,
            season=1,
            username="alice",
            owner_scope=True,
        )

    assert sid == 90
    assert all(
        call.kwargs["episode_group"] == "eg-owner"
        for call in subscribe_model.exists_by_username.call_args_list
    )

    async_persisted = SimpleNamespace(id=91)
    async_created = SimpleNamespace(async_create=AsyncMock())
    with patch("app.db.subscribe_oper.Subscribe") as subscribe_model:
        subscribe_model.async_exists_by_username = AsyncMock(
            side_effect=[None, async_persisted]
        )
        subscribe_model.return_value = async_created

        sid, _ = asyncio.run(SubscribeOper(db=object()).async_add(
            mediainfo=media,
            season=1,
            username="alice",
            owner_scope=True,
        ))

    assert sid == 91
    assert all(
        call.kwargs["episode_group"] == "eg-owner"
        for call in subscribe_model.async_exists_by_username.await_args_list
    )


def test_exists_defaults_to_main_season_episode_group():
    """省略剧集组时按主季查询，显式剧集组按对应范围查询。"""
    oper = SubscribeOper(db=object())
    with patch("app.db.subscribe_oper.Subscribe") as subscribe_model:
        subscribe_model.exists.return_value = SimpleNamespace(id=1)

        assert oper.exists(tmdbid=100, season=1) is True
        assert subscribe_model.exists.call_args.kwargs["episode_group"] is None

        assert oper.exists(tmdbid=100, season=1, episode_group="eg-1") is True
        assert subscribe_model.exists.call_args.kwargs["episode_group"] == "eg-1"

    with patch("app.db.subscribe_oper.SubscribeHistory") as history_model:
        history_model.exists.return_value = SimpleNamespace(id=2)

        assert oper.exist_history(tmdbid=100, season=1) is True
        assert history_model.exists.call_args.kwargs["episode_group"] is None

        assert oper.exist_history(tmdbid=100, season=1, episode_group="eg-1") is True
        assert history_model.exists.call_args.kwargs["episode_group"] == "eg-1"


def test_subscribe_exists_distinguishes_same_season_episode_groups():
    """同一媒体同一季的主季、自定义剧集组应分别命中各自订阅。"""
    oper = SubscribeOper()
    tmdbid = -(900_000_000 + os.getpid())
    created_ids = []
    rows = [
        Subscribe(name="主季订阅", type=MediaType.TV.value, state="N",
                  tmdbid=tmdbid, season=1, episode_group=None),
        Subscribe(name="剧集组订阅", type=MediaType.TV.value, state="N",
                  tmdbid=tmdbid, season=1, episode_group="eg-1"),
    ]
    try:
        for row in rows:
            row.create(oper._db)

        main_season = Subscribe.exists(
            oper._db, tmdbid=tmdbid, season=1, episode_group=None,
        )
        created_ids.append(main_season.id)
        main_name = main_season.name
        episode_group = Subscribe.exists(
            oper._db, tmdbid=tmdbid, season=1, episode_group="eg-1",
        )
        created_ids.append(episode_group.id)
        episode_group_name = episode_group.name

        assert main_name == "主季订阅"
        assert episode_group_name == "剧集组订阅"

        Subscribe.delete(oper._db, rid=created_ids.pop(0))
        assert Subscribe.exists(oper._db, tmdbid=tmdbid, season=1) is None
    finally:
        for subscribe_id in created_ids:
            Subscribe.delete(oper._db, rid=subscribe_id)


def test_subscribe_chain_exists_forwards_episode_group():
    """订阅前置存在性检查必须查询当前剧集组，不能退回主季范围。"""
    from app.chain.subscribe import SubscribeChain

    media = _media("eg-1")
    meta = SimpleNamespace(begin_season=1)
    with patch("app.chain.subscribe.SubscribeOper") as subscribe_oper_cls:
        subscribe_oper_cls.return_value.exists.return_value = True

        assert SubscribeChain.exists(media, meta) is True

    subscribe_oper_cls.return_value.exists.assert_called_once_with(
        tmdbid=media.tmdb_id,
        doubanid=media.douban_id,
        bangumiid=media.bangumi_id,
        anilistid=media.anilist_id,
        media_source="themoviedb",
        media_id=str(media.tmdb_id),
        season=1,
        episode_group="eg-1",
    )
