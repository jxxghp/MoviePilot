"""
订阅表与订阅历史表的查询行为。

订阅身份由「来源 + 原生 ID + 季 + 剧集组 + 音乐实体」五项共同确定，任意一项在查询里
丢失都会造成误判：判为已存在则新订阅被拒绝，判为不存在则同一部剧被重复订阅。
这些都不会抛异常，只能靠对真实数据的断言暴露。
"""
import asyncio
import time as _time

import pytest

from app.db import base as db_base
from app.db.models import subscribe as subscribe_module
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.session import async_session_scope
from app.schemas.types import MediaSource, MediaType

TMDB = str(MediaSource.TMDB)


@pytest.fixture(autouse=True)
def _track(db):
    """把订阅与订阅历史表纳入用例级回收。"""
    db.watermark(Subscribe, SubscribeHistory)


def _sub(name: str, media_id: str = "9001", season: int = 1, episode_group: str = None,
         state: str = "N", username: str = "alice", mtype: str = None,
         music_type: str = None, date: str = "2026-08-13 10:00:00") -> Subscribe:
    """构造一条订阅记录。"""
    return Subscribe(name=name, type=mtype or MediaType.TV.value, state=state,
                     media_source=TMDB, media_id=media_id, season=season,
                     episode_group=episode_group, username=username,
                     music_type=music_type, date=date)


# --------------------------------------------------------------------------- #
# Subscribe：身份查询
# --------------------------------------------------------------------------- #

def test_exists_distinguishes_season_and_episode_group(db):
    """
    同一媒体的不同季、不同剧集组各自是独立订阅身份。

    剧集组条件丢失时，主季订阅会命中自定义剧集组的订阅，用户再也加不上第二个组。
    """
    db.add(_sub("主季", season=1, episode_group=None),
           _sub("剧集组", season=1, episode_group="eg-1"),
           _sub("第二季", season=2, episode_group=None))

    assert Subscribe.exists(db.session, MediaSource.TMDB, "9001", season=1,
                            episode_group=None).name == "主季"
    assert Subscribe.exists(db.session, MediaSource.TMDB, "9001", season=1,
                            episode_group="eg-1").name == "剧集组"
    assert Subscribe.exists(db.session, MediaSource.TMDB, "9001", season=2,
                            episode_group=None).name == "第二季"
    assert Subscribe.exists(db.session, MediaSource.TMDB, "9001", season=3,
                            episode_group=None) is None


def test_exists_matches_async_twin(db):
    """
    同步与异步的身份判定必须一致，否则 API 与调度任务对「是否已订阅」意见相左。
    """
    db.add(_sub("并行", season=1))

    sync_found = Subscribe.exists(db.session, MediaSource.TMDB, "9001", season=1)
    async_found = db.run_async_session(
        lambda session: Subscribe.async_exists(
            session, media_source=MediaSource.TMDB, media_id="9001", season=1
        )
    )

    assert sync_found.id == async_found.id


def test_history_queries_reuse_explicit_sessions(db, monkeypatch):
    """订阅历史同步/异步查询必须复用调用方会话。"""
    row = db.add(_history("显式历史", media_id="8501"))
    monkeypatch.setattr(
        db_base,
        "run_sync_transaction",
        lambda _operation: (_ for _ in ()).throw(
            AssertionError("不应创建额外同步事务")
        ),
    )
    assert SubscribeHistory.list_by_type(
        db.session, MediaType.TV.value, page=1, count=10
    )[0].id == row.id
    assert SubscribeHistory.exists(
        db.session, MediaSource.TMDB, "8501", season=1
    ).id == row.id

    async def check() -> None:
        """验证异步订阅历史查询复用显式 AsyncSession。"""
        async with async_session_scope() as session:
            monkeypatch.setattr(
                db_base,
                "run_async_transaction",
                lambda _operation: (_ for _ in ()).throw(
                    AssertionError("不应创建额外异步事务")
                ),
            )
            assert await SubscribeHistory.async_list_by_type(
                session, MediaType.TV.value, page=1, count=10
            )
            assert await SubscribeHistory.async_list_by_type_and_username(
                session, MediaType.TV.value, "alice", page=1, count=10
            )
            assert await SubscribeHistory.async_exists(
                session, MediaSource.TMDB, "8501", season=1
            ) is not None

    asyncio.run(check())


@pytest.mark.parametrize("media_id", [None, "", "   "])
def test_exists_rejects_blank_media_id(db, media_id):
    """
    媒体 ID 为空时直接返回 None。

    否则条件退化，任意一条订阅都会被当成命中，新订阅全部被拒。
    """
    db.add(_sub("有订阅"))

    assert Subscribe.exists(db.session, MediaSource.TMDB, media_id, season=1) is None


def test_exists_treats_recording_as_matching_null_music_type(db):
    """
    单曲订阅要兼容历史上未写 music_type 的行。

    老数据的 music_type 为空，若严格相等匹配会被判为不存在，用户会重复订阅同一首歌。
    """
    db.add(_sub("老单曲", media_id="mb-1", season=None, music_type=None,
                mtype=MediaType.MUSIC.value))

    found = Subscribe.exists(db.session, MediaSource.TMDB, "mb-1", music_type="recording")

    assert found is not None and found.name == "老单曲"


def test_exists_by_username_scopes_to_owner(db):
    """
    按 owner 查询必须限定用户名，且用户名为空时直接返回 None。
    """
    db.add(_sub("alice 的", username="alice"), _sub("bob 的", username="bob", media_id="9002"))

    assert Subscribe.exists_by_username(db.session, "alice", MediaSource.TMDB,
                                        "9001", season=1).name == "alice 的"
    assert Subscribe.exists_by_username(db.session, "bob", MediaSource.TMDB,
                                        "9001", season=1) is None
    assert Subscribe.exists_by_username(db.session, "", MediaSource.TMDB,
                                        "9001", season=1) is None


def test_get_by_narrows_with_type_and_optional_season(db):
    """
    按类型查询时类型必须参与匹配，季号可选但给出即须生效。
    """
    db.add(_sub("剧集", mtype=MediaType.TV.value, season=1),
           _sub("电影", mtype=MediaType.MOVIE.value, season=1, media_id="9003"))

    assert Subscribe.get_by(db.session, MediaType.TV.value, MediaSource.TMDB,
                            "9001").name == "剧集"
    assert Subscribe.get_by(db.session, MediaType.MOVIE.value, MediaSource.TMDB,
                            "9001") is None
    assert Subscribe.get_by(db.session, MediaType.TV.value, MediaSource.TMDB,
                            "9001", season=2) is None


def test_list_by_media_identity_returns_all_seasons(db):
    """
    按媒体身份列举会跨季返回全部订阅，空身份则短路成空列表。
    """
    db.add(_sub("第一季", season=1), _sub("第二季", season=2),
           _sub("别的剧", media_id="9009"))

    listed = Subscribe.list_by_media_identity(db.session, MediaSource.TMDB, "9001")

    assert {s.season for s in listed} == {1, 2}
    assert Subscribe.list_by_media_identity(db.session, MediaSource.TMDB, "") == []


# --------------------------------------------------------------------------- #
# Subscribe：列表查询
# --------------------------------------------------------------------------- #

def test_get_by_state_splits_comma_separated_states(db):
    """
    状态支持逗号分隔的多值，为空时返回全部。

    订阅刷新按状态取任务，多值解析失效会让一部分订阅永远不被处理。
    """
    db.add(_sub("待订阅", state="N"), _sub("订阅中", state="R", media_id="9004"),
           _sub("已完成", state="P", media_id="9005"))

    states = {s.state for s in Subscribe.get_by_state(db.session, "N,R")}
    assert states == {"N", "R"}

    assert len(Subscribe.get_by_state(db.session, "")) >= 3
    assert {s.state for s in db.run_async_session(
        lambda session: Subscribe.async_get_by_state(session, state="N,R")
    )} == {"N", "R"}


def test_get_by_title_optionally_narrows_by_season(db):
    """
    按标题查询时季号可选，给出即须生效。
    """
    db.add(_sub("同名剧", season=1), _sub("同名剧", season=2))

    assert Subscribe.get_by_title(db.session, "同名剧", season=2).season == 2
    assert Subscribe.get_by_title(db.session, "同名剧") is not None
    assert Subscribe.get_by_title(db.session, "不存在的剧") is None


@pytest.mark.parametrize("state,mtype,expected", [
    (None, None, {"剧-N", "剧-R", "影-N"}),
    ("N", None, {"剧-N", "影-N"}),
    (None, MediaType.TV.value, {"剧-N", "剧-R"}),
    ("N", MediaType.TV.value, {"剧-N"}),
])
def test_list_by_username_covers_all_filter_combinations(db, state, mtype, expected):
    """
    按 owner 列举的四种「状态 × 类型」组合都必须正确收窄。

    这四条分支是「我的订阅」页面的全部筛选路径，任何一条串了都会展示别人的订阅
    或漏掉自己的。
    """
    db.add(_sub("剧-N", state="N", mtype=MediaType.TV.value, media_id="9101"),
           _sub("剧-R", state="R", mtype=MediaType.TV.value, media_id="9102"),
           _sub("影-N", state="N", mtype=MediaType.MOVIE.value, media_id="9103"),
           _sub("别人的", state="N", mtype=MediaType.TV.value, media_id="9104",
                username="bob"))

    listed = Subscribe.list_by_username(db.session, "alice", state=state, mtype=mtype)

    assert {s.name for s in listed} == expected


def test_list_by_username_matches_async_twin(db):
    """
    四种筛选组合下同步与异步必须返回同一批订阅。
    """
    db.add(_sub("剧-N", state="N", mtype=MediaType.TV.value, media_id="9201"),
           _sub("影-R", state="R", mtype=MediaType.MOVIE.value, media_id="9202"))

    for state, mtype in ((None, None), ("N", None), (None, MediaType.TV.value),
                         ("N", MediaType.TV.value)):
        sync_names = sorted(s.name for s in
                            Subscribe.list_by_username(db.session, "alice", state, mtype))
        async_names = sorted(s.name for s in db.run_async_session(
            lambda session: Subscribe.async_list_by_username(
                session, username="alice", state=state, mtype=mtype
            )
        ))
        assert sync_names == async_names


def test_list_by_type_only_returns_recent_days(db):
    """
    按类型取最近 N 天的订阅，超出窗口的不返回。

    时间窗口失效会让「最近订阅」把历史全量拉出来，首页直接卡死。
    """
    db.add(_sub("最近", mtype=MediaType.TV.value, media_id="9301",
                date="2099-01-01 00:00:00"),
           _sub("很久以前", mtype=MediaType.TV.value, media_id="9302",
                date="2000-01-01 00:00:00"))

    names = {s.name for s in Subscribe.list_by_type(db.session, MediaType.TV.value, days=7)}

    assert "最近" in names
    assert "很久以前" not in names


def test_list_by_type_includes_the_window_start_boundary(db, frozen_now):
    """
    时间窗是闭区间起点（``date >= 起点``），正好落在起点的订阅必须在结果里，同步异步一致。

    起点由「调用时刻 - N 天」现算，不冻结时钟就摆不到边界上；上面那条用例用的是
    2099/2000 两个极端值，比较符改成 ``>`` 照样绿。
    """
    now = frozen_now(subscribe_module)
    window_start = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(now - 86400 * 7))
    one_second_earlier = _time.strftime("%Y-%m-%d %H:%M:%S",
                                        _time.localtime(now - 86400 * 7 - 1))
    db.add(_sub("窗口起点上", mtype=MediaType.TV.value, media_id="9303", date=window_start),
           _sub("窗口起点前一秒", mtype=MediaType.TV.value, media_id="9304",
                date=one_second_earlier))

    names = {s.name for s in Subscribe.list_by_type(db.session, MediaType.TV.value, days=7)}
    async_names = {s.name for s in db.run_async_session(
        lambda session: Subscribe.async_list_by_type(
            session, mtype=MediaType.TV.value, days=7
        )
    )}

    assert "窗口起点上" in names and "窗口起点前一秒" not in names
    assert "窗口起点上" in async_names and "窗口起点前一秒" not in async_names


# --------------------------------------------------------------------------- #
# SubscribeHistory
# --------------------------------------------------------------------------- #

def _history(name: str, mtype: str = MediaType.TV.value, media_id: str = "8001",
             season: int = 1, episode_group: str = None,
             date: str = "2026-08-13 10:00:00", username: str = "alice") -> SubscribeHistory:
    """构造一条订阅历史记录。"""
    return SubscribeHistory(name=name, type=mtype, media_source=TMDB, media_id=media_id,
                            season=season, episode_group=episode_group, date=date,
                            username=username)


def test_history_list_by_type_is_newest_first_and_paged(db):
    """
    历史按完成时间倒序分页，且只返回指定类型。
    """
    db.add(_history("旧", date="2026-08-01 10:00:00", media_id="8101"),
           _history("新", date="2026-08-12 10:00:00", media_id="8102"),
           _history("电影", mtype=MediaType.MOVIE.value, media_id="8103"))

    page = SubscribeHistory.list_by_type(db.session, MediaType.TV.value, page=1, count=10)

    assert [h.name for h in page] == ["新", "旧"]
    assert [h.name for h in SubscribeHistory.list_by_type(
        db.session, MediaType.TV.value, page=1, count=1)] == ["新"]


def test_history_list_by_type_matches_async_twin(db):
    """
    同步与异步的历史分页必须返回同一批记录。
    """
    db.add(_history("A", date="2026-08-12 10:00:00", media_id="8201"),
           _history("B", date="2026-08-11 10:00:00", media_id="8202"))

    sync_names = [h.name for h in SubscribeHistory.list_by_type(
        db.session, MediaType.TV.value, page=1, count=10)]
    async_names = [h.name for h in db.run_async_session(
        lambda session: SubscribeHistory.async_list_by_type(
            session, mtype=MediaType.TV.value, page=1, count=10
        )
    )]

    assert sync_names == async_names


def test_history_exists_distinguishes_episode_group(db):
    """
    历史的存在性判定与订阅同规则：剧集组不同即为不同身份。

    判错会让已完成的主季订阅挡住自定义剧集组的新订阅。
    """
    db.add(_history("主季历史", season=1, episode_group=None, media_id="8301"),
           _history("剧集组历史", season=1, episode_group="eg-1", media_id="8301"))

    assert SubscribeHistory.exists(db.session, MediaSource.TMDB, "8301", season=1,
                                   episode_group=None).name == "主季历史"
    assert SubscribeHistory.exists(db.session, MediaSource.TMDB, "8301", season=1,
                                   episode_group="eg-1").name == "剧集组历史"
    assert SubscribeHistory.exists(db.session, MediaSource.TMDB, "", season=1) is None


def test_history_exists_matches_async_twin(db):
    """
    历史存在性判定的同步与异步结果必须一致。
    """
    db.add(_history("并行历史", season=1, media_id="8401"))

    sync_found = SubscribeHistory.exists(db.session, MediaSource.TMDB, "8401", season=1)
    async_found = db.run_async_session(
        lambda session: SubscribeHistory.async_exists(
            session,
            media_source=MediaSource.TMDB,
            media_id="8401",
            season=1,
        )
    )

    assert sync_found.id == async_found.id
