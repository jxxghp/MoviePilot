"""
站点相关四张表的查询行为：站点、图标、访问统计、用户数据快照。

站点数据快照的 get_latest 是这里唯一带子查询与 JOIN 的查询——「每个站点取最新一天」
用普通过滤写不出来，改写时最容易退化成「全表按时间倒序取第一条」，那样多站点场景下
只会剩一个站点的数据，而页面不会报错，只是少了几行。
"""
import asyncio

import pytest

from app.db import base as db_base
from app.db.models.site import Site
from app.db.models.siteicon import SiteIcon
from app.db.models.sitestatistic import SiteStatistic
from app.db.models.siteuserdata import SiteUserData
from app.db.oper.site import SiteOper
from app.db.session import async_session_scope


@pytest.fixture(autouse=True)
def _track(db):
    """把站点相关表纳入用例级回收。"""
    db.watermark(Site, SiteIcon, SiteStatistic, SiteUserData)


def _site(name: str, domain: str, pri: int = 1, is_active: bool = True) -> Site:
    """构造一条站点记录。"""
    return Site(name=name, domain=domain, url=f"https://{domain}/", pri=pri, is_active=is_active)


# --------------------------------------------------------------------------- #
# Site
# --------------------------------------------------------------------------- #

def test_site_get_by_domain_matches_async_twin(db):
    """
    按域名取站点的同步、异步结果必须指向同一行。
    """
    db.add(_site("站点A", "a.test"), _site("站点B", "b.test"))

    assert Site.get_by_domain(db.session, "a.test").name == "站点A"
    assert db.run_async_session(
        lambda session: Site.async_get_by_domain(session, "a.test")
    ).name == "站点A"
    assert db.run_async_session(
        lambda session: Site.async_get_by_name(session, "站点B")
    ).domain == "b.test"


def test_site_get_by_domain_returns_none_when_absent(db):
    """
    域名不存在时返回 None，调用方据此判断站点是否已配置。
    """
    assert Site.get_by_domain(db.session, "missing.test") is None


def test_site_get_actives_excludes_disabled_sites(db):
    """
    取启用站点必须排除已停用的。

    停用站点仍被返回意味着它照样会被搜索和刷流访问，等于停用开关没生效。
    """
    db.add(_site("启用1", "on1.test"), _site("启用2", "on2.test"),
           _site("停用", "off.test", is_active=False))

    assert {s.domain for s in Site.get_actives(db.session)} == {"on1.test", "on2.test"}
    assert {s.domain for s in db.run_async_session(Site.async_get_actives)} == {
        "on1.test", "on2.test"
    }


def test_site_list_order_by_pri_is_ascending(db):
    """
    站点列表必须按优先级升序——顺序决定搜索与下载的站点先后。
    """
    db.add(_site("三", "p3.test", pri=3), _site("一", "p1.test", pri=1),
           _site("二", "p2.test", pri=2))

    assert [s.domain for s in Site.list_order_by_pri(db.session)] == \
        ["p1.test", "p2.test", "p3.test"]
    assert [s.domain for s in db.run_async_session(Site.async_list_order_by_pri)] == \
        ["p1.test", "p2.test", "p3.test"]


def test_site_get_domains_by_ids_returns_plain_strings(db):
    """
    按 ID 批量取域名必须返回纯字符串列表，且只含请求的那些 ID。
    """
    first = db.add(_site("一", "d1.test"))
    second = db.add(_site("二", "d2.test"))
    db.add(_site("三", "d3.test"))

    domains = Site.get_domains_by_ids(db.session, [first.id, second.id])

    assert sorted(domains) == ["d1.test", "d2.test"]
    assert all(isinstance(item, str) for item in domains)


def test_site_get_domains_by_ids_with_empty_list(db):
    """
    ID 列表为空时返回空列表，不能退化成返回全部域名。
    """
    db.add(_site("一", "e1.test"))

    assert Site.get_domains_by_ids(db.session, []) == []


def test_site_reset_empties_the_table(db):
    """
    重置会清空站点表——CookieCloud 全量同步依赖它先清场再写入。
    """
    db.add(_site("一", "r1.test"))

    Site.reset(db.session)

    assert Site.list_order_by_pri(db.session) == []


# --------------------------------------------------------------------------- #
# SiteIcon / SiteStatistic
# --------------------------------------------------------------------------- #

def test_siteicon_get_by_domain_matches_async_twin(db):
    """
    图标按域名查找的同步、异步结果必须一致。
    """
    db.add(SiteIcon(name="站点A", domain="icon-a.test", url="https://icon-a.test/f.ico"),
           SiteIcon(name="站点B", domain="icon-b.test", url="https://icon-b.test/f.ico"))

    assert SiteIcon.get_by_domain(db.session, "icon-a.test").name == "站点A"
    assert db.run_async_session(
        lambda session: SiteIcon.async_get_by_domain(session, "icon-a.test")
    ).name == "站点A"
    assert SiteIcon.get_by_domain(db.session, "icon-missing.test") is None


def test_sitestatistic_get_by_domain_matches_async_twin(db):
    """
    访问统计按域名查找的同步、异步结果必须一致。
    """
    db.add(SiteStatistic(domain="stat-a.test", success=3, fail=1, seconds=2, lst_state=0),
           SiteStatistic(domain="stat-b.test", success=1, fail=0, seconds=1, lst_state=0))

    assert SiteStatistic.get_by_domain(db.session, "stat-a.test").success == 3
    assert db.run_async_session(
        lambda session: SiteStatistic.async_get_by_domain(session, "stat-a.test")
    ).success == 3
    assert SiteStatistic.get_by_domain(db.session, "stat-missing.test") is None


def test_sitestatistic_reset_empties_the_table(db):
    """
    重置统计会清空整表，供「重置站点数据」入口使用。
    """
    db.add(SiteStatistic(domain="stat-reset.test", success=1, fail=0, seconds=1, lst_state=0))

    SiteStatistic.reset(db.session)

    assert SiteStatistic.get_by_domain(db.session, "stat-reset.test") is None


# --------------------------------------------------------------------------- #
# SiteUserData
# --------------------------------------------------------------------------- #

def _userdata(domain: str, day: str, time: str, upload: float = 0,
              err_msg: str = None) -> SiteUserData:
    """构造一条站点用户数据快照。"""
    return SiteUserData(domain=domain, name=domain, username="u", upload=upload,
                        updated_day=day, updated_time=time, err_msg=err_msg)


def test_userdata_get_by_domain_narrows_with_date_and_time(db):
    """
    按域名查询时，日期与时刻参数应逐级收窄结果范围。
    """
    db.add(_userdata("ud.test", "2026-08-11", "10:00:00"),
           _userdata("ud.test", "2026-08-12", "10:00:00"),
           _userdata("ud.test", "2026-08-12", "20:00:00"),
           _userdata("other.test", "2026-08-12", "10:00:00"))

    assert len(SiteUserData.get_by_domain(db.session, "ud.test")) == 3
    assert len(SiteUserData.get_by_domain(db.session, "ud.test", workdate="2026-08-12")) == 2
    assert len(SiteUserData.get_by_domain(db.session, "ud.test",
                                          workdate="2026-08-12", worktime="20:00:00")) == 1


def test_userdata_get_by_domain_matches_async_twin(db):
    """
    三种收窄组合下同步与异步必须给出同样多的行。
    """
    db.add(_userdata("ud2.test", "2026-08-12", "10:00:00"),
           _userdata("ud2.test", "2026-08-12", "20:00:00"))

    for kwargs in ({}, {"workdate": "2026-08-12"},
                   {"workdate": "2026-08-12", "worktime": "20:00:00"}):
        sync_rows = SiteUserData.get_by_domain(db.session, "ud2.test", **kwargs)
        async_rows = db.run_async_session(
            lambda session: SiteUserData.async_get_by_domain(
                session, domain="ud2.test", **kwargs
            )
        )
        assert len(sync_rows) == len(async_rows)


def test_site_oper_reuses_explicit_userdata_query_sessions(db, monkeypatch):
    """站点用户数据 Oper 必须复用调用方同步与异步会话。"""
    db.add(_userdata("explicit-site.test", "2026-08-12", "10:00:00"))
    monkeypatch.setattr(
        db_base,
        "run_sync_transaction",
        lambda _operation: (_ for _ in ()).throw(
            AssertionError("不应创建额外同步事务")
        ),
    )

    assert SiteOper(db.session).get_userdata_by_domain("explicit-site.test")

    async def check() -> None:
        """验证异步站点用户数据查询复用显式 AsyncSession。"""
        async with async_session_scope() as session:
            monkeypatch.setattr(
                db_base,
                "run_async_transaction",
                lambda _operation: (_ for _ in ()).throw(
                    AssertionError("不应创建额外异步事务")
                ),
            )
            assert await SiteOper(session).async_get_userdata_by_domain(
                "explicit-site.test"
            )

    asyncio.run(check())


def test_userdata_get_by_date_returns_all_domains_of_that_day(db):
    """
    按日期查询应跨站点返回当天全部快照。
    """
    db.add(_userdata("day-a.test", "2026-08-12", "10:00:00"),
           _userdata("day-b.test", "2026-08-12", "10:00:00"),
           _userdata("day-a.test", "2026-08-11", "10:00:00"))

    rows = SiteUserData.get_by_date(db.session, "2026-08-12")

    assert {r.domain for r in rows} == {"day-a.test", "day-b.test"}


def test_userdata_get_latest_returns_one_day_per_domain(db):
    """
    每个站点只返回其最新一天的快照，且跨站点互不影响。

    这条正是子查询存在的理由：退化成「全表取最新」时，只会剩下日期最大的那个站点。
    """
    db.add(_userdata("late-a.test", "2026-08-10", "10:00:00", upload=1),
           _userdata("late-a.test", "2026-08-12", "10:00:00", upload=2),
           _userdata("late-b.test", "2026-08-11", "10:00:00", upload=3))

    latest = {r.domain: r for r in SiteUserData.get_latest(db.session)
              if r.domain in ("late-a.test", "late-b.test")}

    assert set(latest) == {"late-a.test", "late-b.test"}
    assert latest["late-a.test"].updated_day == "2026-08-12"
    assert latest["late-b.test"].updated_day == "2026-08-11"


def test_userdata_get_latest_ignores_failed_snapshots_when_picking_the_day(db):
    """
    带错误信息的快照不参与「最新一天」的判定。

    抓取失败当天也会留一条记录，若它决定了最新日期，站点数据会显示成空。
    """
    db.add(_userdata("err.test", "2026-08-10", "10:00:00", upload=5),
           _userdata("err.test", "2026-08-12", "10:00:00", err_msg="登录失败"))

    rows = [r for r in SiteUserData.get_latest(db.session) if r.domain == "err.test"]

    assert [r.updated_day for r in rows] == ["2026-08-10"]


def test_userdata_get_latest_matches_async_twin(db):
    """
    同步与异步的「最新一天」必须选出同一批行。
    """
    db.add(_userdata("par.test", "2026-08-10", "10:00:00"),
           _userdata("par.test", "2026-08-12", "10:00:00"))

    sync_rows = [(r.domain, r.updated_day) for r in SiteUserData.get_latest(db.session)]
    async_rows = [
        (r.domain, r.updated_day)
        for r in db.run_async_session(SiteUserData.async_get_latest)
    ]

    assert sorted(sync_rows) == sorted(async_rows)


def test_userdata_delete_before_is_batched_and_bounded(db):
    """
    清理旧快照必须分批并遵守上限，且不碰保留期内的数据。

    一次性删除大表会长时间持锁，SQLite 下直接表现为整个应用卡住。
    """
    for index in range(5):
        db.add(_userdata("old.test", "2026-01-0%d" % (index + 1), "10:00:00"))
    db.add(_userdata("old.test", "2026-08-12", "10:00:00"))

    assert SiteUserData.delete_before(db.session, before_day="2026-08-01", limit=2) == 2
    assert SiteUserData.delete_before(db.session, before_day="2026-08-01", limit=100) == 3
    assert SiteUserData.delete_before(db.session, before_day="2026-08-01", limit=100) == 0

    remaining = SiteUserData.get_by_domain(db.session, "old.test")
    assert [r.updated_day for r in remaining] == ["2026-08-12"]


def test_userdata_delete_before_keeps_the_row_exactly_at_the_boundary(db):
    """
    保留日期当天的快照属于「保留期内」，不能被清理（``updated_day < before_day``）。

    上面那条用例的数据离水位有半年之遥，``<`` 写成 ``<=`` 也照样绿；
    这里把行压在水位当天，让开闭区间之差可观测——差一天就是少一天的站点数据曲线。
    """
    boundary = "2026-05-01"
    db.add(_userdata("boundary.test", boundary, "10:00:00"),
           _userdata("boundary.test", "2026-04-30", "10:00:00"))

    assert SiteUserData.delete_before(db.session, before_day=boundary, limit=100) == 1

    remaining = SiteUserData.get_by_domain(db.session, "boundary.test")
    assert [r.updated_day for r in remaining] == [boundary]
