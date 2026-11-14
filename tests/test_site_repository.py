"""
站点 SQLAlchemy 适配器的快照与事务边界测试。
"""

import pytest

from app.application.security.image import SiteImageDomainCache
from app.application.site.contract import (
    SiteMutation,
    SitePriorityMutation,
    SiteSnapshot,
    SiteStatisticSnapshot,
    SiteUserDataMutation,
    SiteUserDataSnapshot,
)
from app.application.site.query import SiteQueryService
from app.db.adapters import site as site_adapter
from app.db.adapters.site import SessionSiteRepository, TransactionalSiteRepository
from app.db.models.site import Site
from app.db.models.siteicon import SiteIcon
from app.db.models.sitestatistic import SiteStatistic
from app.db.models.siteuserdata import SiteUserData
from app.db.oper.site import SiteOper
from app.db.session import SessionFactory, async_session_scope
from app.db.uow import SqlAlchemyAsyncUnitOfWork


def _transactional_repository() -> TransactionalSiteRepository:
    """
    按生产组合方式构造短 Session 站点仓储。
    """
    return TransactionalSiteRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )


@pytest.mark.asyncio
async def test_site_writes_refresh_image_domains(db, monkeypatch) -> None:
    """
    同步新增、同步改址和异步改址提交后均撤销旧图片域名快照。
    """
    db.watermark(Site)
    cache = SiteImageDomainCache()
    monkeypatch.setattr(site_adapter, "site_image_domains", cache)
    repository = _transactional_repository()
    source = SiteQueryService(repository=repository)
    original = await cache.get(source)
    assert repository.add(SiteMutation({
        "name": "图片缓存", "domain": "image-cache.example", "url": "https://old-image.example/",
    })).success
    snapshot = repository.get_by_domain("image-cache.example")
    assert snapshot is not None
    assert "https://old-image.example" in await cache.get(source) - original
    repository.update(snapshot.id, SiteMutation({"url": "https://new-image.example/"}))
    domains = await cache.get(source)
    assert "https://old-image.example" not in domains
    assert "https://new-image.example" in domains
    await repository.async_update(snapshot.id, SiteMutation({"url": "https://async-image.example/"}))
    domains = await cache.get(source)
    assert "https://new-image.example" not in domains
    assert "https://async-image.example" in domains


def test_transactional_repository_projects_frozen_site_snapshot(db) -> None:
    """
    独立仓储应在 Session 内复制 JSON 并返回不可变站点快照。
    """
    db.watermark(Site)
    note = {"nested": ["before"]}
    repository = _transactional_repository()

    result = repository.add(
        SiteMutation(
            {
                "name": "快照站点",
                "domain": "typed-site.example",
                "url": "https://typed-site.example/",
                "note": note,
                "is_active": True,
            }
        )
    )
    note["nested"].append("after")
    snapshot = repository.get_by_domain("typed-site.example")

    assert result.success is True
    assert isinstance(snapshot, SiteSnapshot)
    assert snapshot.note == {"nested": ["before"]}
    with pytest.raises(TypeError, match="不可修改"):
        # 主动修改快照中的嵌套数据，验证对外投影与持久对象隔离。
        snapshot.note["new"] = True  # type: ignore[index]


@pytest.mark.asyncio
async def test_transactional_repository_projects_related_snapshots(db) -> None:
    """
    用户数据、图标与统计查询均不得把 ORM 对象带出 Session。
    """
    db.add(
        SiteUserData(
            domain="related-site.example",
            name="关联站点",
            seeding_info={"size": [1, 2]},
            message_unread_contents=[{"title": "notice"}],
            err_msg="",
        ),
        SiteIcon(
            name="关联站点",
            domain="related-site.example",
            url="https://related-site.example/favicon.ico",
        ),
        SiteStatistic(
            domain="related-site.example",
            success=2,
            fail=1,
            note={"2026-08-28 10:00:00": 3},
        ),
    )
    repository = _transactional_repository()

    userdata = await repository.async_get_userdata_by_domain("related-site.example")
    icon = await repository.async_get_icon_by_domain("related-site.example")
    statistic = await repository.async_get_statistic_by_domain("related-site.example")

    assert isinstance(userdata[0], SiteUserDataSnapshot)
    assert userdata[0].seeding_info == {"size": [1, 2]}
    assert icon is not None and icon.domain == "related-site.example"
    assert isinstance(statistic, SiteStatisticSnapshot)
    assert statistic.note == {"2026-08-28 10:00:00": 3}


@pytest.mark.parametrize(
    ("ratio_fields", "expected_ratio"),
    [({}, 0), ({"ratio": None}, None), ({"ratio": 0}, 0), ({"ratio": 1.23}, 1.23)],
)
def test_userdata_ratio_preserves_null_and_default_through_storage(db, ratio_fields, expected_ratio) -> None:
    """
    首次写入与当天更新均保留显式空分享率，省略字段仍使用默认零值。
    """
    db.watermark(SiteUserData)
    domain = "nullable-ratio.example"
    repository = _transactional_repository()
    assert repository.update_userdata(
        domain, "空分享率站点", SiteUserDataMutation({"download": 0, **ratio_fields}),
    ).success

    def read_ratio():
        """
        通过生产查询 DTO 读取已提交的分享率。
        """
        records = SiteQueryService(repository).userdata_latest_sync()
        return next(item.ratio for item in records if item.domain == domain)

    assert read_ratio() == expected_ratio
    repository.update_userdata(domain, "空分享率站点", SiteUserDataMutation({"ratio": 2.5}))
    assert read_ratio() == 2.5
    repository.update_userdata(domain, "空分享率站点", SiteUserDataMutation({"ratio": None}))
    assert read_ratio() is None


def test_userdata_default_seeding_info_is_compatible_with_sync_query_dto(db) -> None:
    """
    省略做种明细时，查询 DTO 应收到空列表而不是 JSON 对象。
    """
    db.watermark(SiteUserData)
    domain = "default-seeding-info.example"
    db.add(SiteUserData(domain=domain, name="默认做种明细站点", err_msg=""))

    result = SiteQueryService(_transactional_repository()).userdata_latest_sync()

    userdata = next(item for item in result if item.domain == domain)
    assert userdata.seeding_info == []


@pytest.mark.asyncio
async def test_session_repository_leaves_commit_and_rollback_to_caller(db) -> None:
    """
    请求级仓储仅暂存，外层回滚和提交应分别决定最终可见性。
    """
    db.watermark(Site)
    repository = _transactional_repository()
    domain = "request-site.example"

    async with async_session_scope() as session:
        request_repository = SessionSiteRepository(session)
        await request_repository.stage_create(
            SiteMutation(
                {
                    "name": "请求站点",
                    "domain": domain,
                    "url": f"https://{domain}/",
                    "pri": 1,
                }
            )
        )
        assert await request_repository.async_get_by_domain(domain) is not None
        await session.rollback()

    assert repository.get_by_domain(domain) is None

    async with async_session_scope() as session:
        request_repository = SessionSiteRepository(session)
        await request_repository.stage_create(
            SiteMutation(
                {
                    "name": "请求站点",
                    "domain": domain,
                    "url": f"https://{domain}/",
                    "pri": 1,
                }
            )
        )
        await SqlAlchemyAsyncUnitOfWork(session).commit()

    snapshot = repository.get_by_domain(domain)
    assert snapshot is not None and snapshot.pri == 1

    async with async_session_scope() as session:
        request_repository = SessionSiteRepository(session)
        await request_repository.stage_priorities((SitePriorityMutation(site_id=snapshot.id, priority=8),))
        await SqlAlchemyAsyncUnitOfWork(session).commit()

    # 本用例已构造非空数据，直接检查或修改其内容以验证快照契约。
    assert repository.get(snapshot.id).pri == 8  # type: ignore[union-attr]


@pytest.mark.parametrize("previous", [None, "", "https://rss-save.example/rss?passkey=OLD"])
def test_rss_compare_update_commits_with_nullable_fields(db, previous) -> None:
    """
    真实 SQL 条件正确匹配空字段，缺失补抓和旧链接续期都在返回成功前提交。
    """
    db.add(Site(name="RSS保存", domain="rss-save.example", url="https://rss-save.example/",
                rss=previous, cookie="FAKECOOKIE"))
    repository = _transactional_repository()
    original = repository.get_by_domain("rss-save.example")
    assert original is not None
    assert original.proxy is None and original.token is None

    result = repository.update_rss_if_unchanged(original, "https://rss-save.example/rss?passkey=NEW")

    assert result.success
    # 另开短会话读取，避免测试被当前 Session 的对象缓存误导。
    saved = _transactional_repository().get(original.id)
    assert saved is not None and saved.rss == "https://rss-save.example/rss?passkey=NEW"
    assert not repository.update_rss_if_unchanged(original, "https://rss-save.example/rss?passkey=STALE").success


@pytest.mark.parametrize("mutation", [
    {"rss": "https://rss-race.example/manual"}, {"cookie": "FAKENEWCOOKIE"},
    {"url": "https://new-rss-race.example/"}, {"ua": "New UA"}, {"apikey": "FAKEAPI"},
    {"token": "FAKETOKEN"}, {"proxy": 1}, {"timeout": 60}, {"render": 1},
    {"public": 1}, {"is_active": False},
])
def test_rss_compare_update_rejects_changed_configuration(db, mutation) -> None:
    """
    网络请求期间发生认证、访问配置或手工 RSS 修改时，单条条件更新不能覆盖新配置。
    """
    db.add(Site(name="RSS并发", domain="rss-race.example", url="https://rss-race.example/",
                rss="https://rss-race.example/rss?passkey=OLD", cookie="FAKECOOKIE"))
    repository = _transactional_repository()
    original = repository.get_by_domain("rss-race.example")
    assert original is not None
    changed = repository.update(original.id, SiteMutation(mutation))
    assert changed is not None

    result = repository.update_rss_if_unchanged(original, "https://rss-race.example/rss?passkey=NEW")

    assert not result.success
    assert repository.get(original.id) == changed


@pytest.mark.parametrize("recreate", [False, True])
def test_rss_compare_update_rejects_deleted_or_replaced_site(db, recreate) -> None:
    """
    删除站点或在同域名重建记录后，旧任务不得将新地址写入替代记录。
    """
    record = db.add(Site(name="RSS删除", domain="rss-delete.example", url="https://rss-delete.example/",
                         cookie="FAKECOOKIE"))
    repository = _transactional_repository()
    original = repository.get(record.id)
    assert original is not None
    db.session.delete(record)
    db.session.commit()
    if recreate:
        db.add(Site(id=original.id + 1, name="RSS重建", domain=original.domain, url=original.url,
                    cookie=original.cookie))

    assert not repository.update_rss_if_unchanged(original, "https://rss-delete.example/rss?passkey=NEW").success
    current = repository.get_by_domain("rss-delete.example")
    if recreate:
        assert current is not None and current.rss is None
    else:
        assert current is None


def test_rss_oper_leaves_transaction_to_caller(db) -> None:
    """
    Oper 的条件写入只暂存，调用方回滚后原地址仍可被独立会话读取。
    """
    record = db.add(Site(name="RSS回滚", domain="rss-rollback.example", url="https://rss-rollback.example/"))
    with SessionFactory() as session:
        result = SiteOper(db=session).update_rss_if_unchanged(
            record.id, "https://rss-rollback.example/rss?passkey=NEW", {"rss": None},
        )
        assert result[0]
        session.rollback()

    saved = _transactional_repository().get(record.id)
    assert saved is not None and saved.rss is None
