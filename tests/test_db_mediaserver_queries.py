"""
媒体服务器条目表的查询行为。

这张表是「媒体库里已经有了吗」的唯一依据：查不到会重复下载，误命中会漏订阅。
同步清理（delete_stale / delete_excluded_servers）的条件一旦写反，就会把当次刚同步
进来的条目全删掉，表现为媒体库突然清空。
"""
import asyncio

import pytest

from app.db import decorators
from app.db.models.mediaserver import MediaServerItem
from app.db.oper.mediaserver import MediaServerOper
from app.db.session import SessionFactory, async_session_scope
from app.schemas.types import MediaSource


@pytest.fixture(autouse=True)
def _track(db):
    """把媒体服务器条目表纳入用例级回收。"""
    db.watermark(MediaServerItem)


def _item(server: str, item_id: str, title: str = "片名", item_type: str = "电影",
          year: str = "2026", media_id: str = "1001",
          lst_mod_date: str = "2026-08-13 10:00:00") -> MediaServerItem:
    """构造一条媒体服务器条目。"""
    return MediaServerItem(server=server, library="lib", item_id=item_id,
                           item_type=item_type, title=title, year=year,
                           media_source=str(MediaSource.TMDB), media_id=media_id,
                           lst_mod_date=lst_mod_date)


def test_get_by_itemid_matches_async_twin(db):
    """
    按条目 ID 查找的同步、异步结果必须指向同一行。
    """
    db.add(_item("emby", "it-1"), _item("plex", "it-2"))

    assert MediaServerItem.get_by_itemid(db.session, "it-1").server == "emby"
    assert asyncio.run(MediaServerItem.async_get_by_itemid(item_id="it-1")).server == "emby"
    assert MediaServerItem.get_by_itemid(db.session, "it-missing") is None


def test_mediaserver_oper_reuses_explicit_query_sessions(db, monkeypatch):
    """媒体服务器 Oper 绑定调用方会话后不得再创建兼容查询会话。"""
    db.add(_item("emby", "explicit-ms", media_id="explicit-1001"))
    monkeypatch.setattr(
        decorators,
        "ScopedSession",
        lambda: (_ for _ in ()).throw(AssertionError("不应创建额外同步会话")),
    )

    assert MediaServerOper(db.session).exists(
        media_source=MediaSource.TMDB,
        media_id="explicit-1001",
        mtype="电影",
    ) is not None

    async def check() -> None:
        """验证异步存在性查询复用显式 AsyncSession。"""
        async with async_session_scope() as session:
            monkeypatch.setattr(
                decorators,
                "async_session_scope",
                lambda: (_ for _ in ()).throw(AssertionError("不应创建额外异步会话")),
            )
            assert await MediaServerOper(session).async_exists(
                media_source=MediaSource.TMDB,
                media_id="explicit-1001",
                mtype="电影",
            ) is not None

    asyncio.run(check())


def test_mediaserver_model_legacy_query_keeps_keyword_abi(db, monkeypatch):
    """旧插件以关键字直调媒体服务器 Model 时仍自动补入短会话。"""
    db.add(_item("emby", "legacy-ms"))
    opened = []
    monkeypatch.setattr(
        decorators,
        "ScopedSession",
        lambda: (opened.append(True) or SessionFactory()),
    )

    assert MediaServerItem.get_by_itemid(item_id="legacy-ms") is not None
    assert opened == [True]


def test_get_by_server_itemid_scopes_by_server(db):
    """
    条目 ID 只在单个服务器内唯一，查找必须同时限定服务器。

    不限定会在多媒体服务器场景下把 Emby 的条目当成 Plex 的，路径与库信息全错。
    """
    db.add(_item("emby", "same-id", title="Emby 的片"),
           _item("plex", "same-id", title="Plex 的片"))

    assert MediaServerItem.get_by_server_itemid(db.session, "emby", "same-id").title == "Emby 的片"
    assert MediaServerItem.get_by_server_itemid(db.session, "plex", "same-id").title == "Plex 的片"
    assert MediaServerItem.get_by_server_itemid(db.session, "jellyfin", "same-id") is None


def test_exist_by_media_identity_requires_source_id_and_type(db):
    """
    按媒体身份判存在必须三项齐同：来源、原生 ID、条目类型。

    忽略类型会让同一 ID 的电影与剧集互相命中，订阅据此判定「已入库」而跳过。
    """
    db.add(_item("emby", "mi-1", media_id="555", item_type="电影"))

    assert MediaServerItem.exist_by_media_identity(
        db.session, MediaSource.TMDB, "555", "电影") is not None
    assert MediaServerItem.exist_by_media_identity(
        db.session, MediaSource.TMDB, "555", "电视剧") is None
    assert MediaServerItem.exist_by_media_identity(
        db.session, MediaSource.TMDB, "556", "电影") is None

    assert asyncio.run(MediaServerItem.async_exist_by_media_identity(
        media_source=MediaSource.TMDB, media_id="555", mtype="电影")) is not None


@pytest.mark.parametrize("mtype,year,expected", [
    (None, None, "标题匹配"),
    ("电影", None, "标题匹配"),
    (None, "2026", "标题匹配"),
    ("电影", "2026", "标题匹配"),
    ("电视剧", "2026", None),
    ("电影", "2020", None),
])
def test_exists_by_title_narrows_by_type_and_year(db, mtype, year, expected):
    """
    按标题判存在时，类型与年份各自可选，给出即须生效。

    这四条分支是同名不同年、同名不同类型的唯一区分手段，退化成只按标题匹配会把
    《XX 2020》当成《XX 2026》，订阅直接被跳过。
    """
    db.add(_item("emby", "t-1", title="标题匹配", item_type="电影", year="2026"))

    found = MediaServerItem.exists_by_title(db.session, "标题匹配", mtype, year)

    assert (found.title if found else None) == expected


def test_exists_by_title_matches_async_twin(db):
    """
    四种参数组合下同步与异步必须给出相同的命中结果。
    """
    db.add(_item("emby", "t-par", title="并行标题", item_type="电影", year="2026"))

    for mtype, year in ((None, None), ("电影", None), (None, "2026"), ("电影", "2026")):
        sync_found = MediaServerItem.exists_by_title(db.session, "并行标题", mtype, year)
        async_found = asyncio.run(MediaServerItem.async_exists_by_title(
            title="并行标题", mtype=mtype, year=year))
        assert (sync_found is None) == (async_found is None)


def test_empty_clears_only_the_given_server(db):
    """
    指定服务器时只清空该服务器的条目，不给则清空全表。

    误清其他服务器的条目会让那台服务器的媒体库在本次同步前一直显示为空。
    """
    db.add(_item("emby", "e-1"), _item("plex", "p-1"))

    MediaServerItem.empty(db.session, server="emby")

    assert MediaServerItem.get_by_itemid(db.session, "e-1") is None
    assert MediaServerItem.get_by_itemid(db.session, "p-1") is not None

    MediaServerItem.empty(db.session)
    assert MediaServerItem.get_by_itemid(db.session, "p-1") is None


def test_delete_stale_keeps_items_from_the_current_sync(db):
    """
    清理陈旧条目时必须保留本次同步时间戳的条目。

    条件写反会把刚同步进来的条目全删掉，媒体库表现为同步完反而空了。
    """
    db.add(_item("emby", "fresh", lst_mod_date="2026-08-13 12:00:00"),
           _item("emby", "stale", lst_mod_date="2026-08-01 12:00:00"),
           _item("emby", "never", lst_mod_date=None),
           _item("plex", "other", lst_mod_date="2026-08-01 12:00:00"))

    deleted = MediaServerItem.delete_stale(db.session, server="emby",
                                           sync_time="2026-08-13 12:00:00")

    assert deleted == 2
    assert MediaServerItem.get_by_itemid(db.session, "fresh") is not None
    assert MediaServerItem.get_by_itemid(db.session, "stale") is None
    assert MediaServerItem.get_by_itemid(db.session, "never") is None
    assert MediaServerItem.get_by_itemid(db.session, "other") is not None


def test_delete_excluded_servers_keeps_configured_ones(db):
    """
    只保留仍在配置中的服务器条目，未配置与来源为空的条目应被清掉。
    """
    db.add(_item("emby", "keep-1"), _item("plex", "drop-1"),
           _item(None, "drop-null"))

    deleted = MediaServerItem.delete_excluded_servers(db.session, ["emby"])

    assert deleted == 2
    assert MediaServerItem.get_by_itemid(db.session, "keep-1") is not None
    assert MediaServerItem.get_by_itemid(db.session, "drop-1") is None
    assert MediaServerItem.get_by_itemid(db.session, "drop-null") is None


def test_delete_excluded_servers_with_empty_list_clears_everything(db):
    """
    一个服务器都没配置时清空全表——否则会留下再也不会被同步到的孤儿条目。
    """
    db.add(_item("emby", "orphan-1"), _item("plex", "orphan-2"))

    MediaServerItem.delete_excluded_servers(db.session, [])

    assert MediaServerItem.get_by_itemid(db.session, "orphan-1") is None
    assert MediaServerItem.get_by_itemid(db.session, "orphan-2") is None
