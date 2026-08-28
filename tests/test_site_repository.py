"""站点 SQLAlchemy 适配器的快照与事务边界测试。"""

import pytest

from app.application.site.contract import (
    SiteMutation,
    SitePriorityMutation,
    SiteSnapshot,
    SiteStatisticSnapshot,
    SiteUserDataSnapshot,
)
from app.db.adapters.site import SessionSiteRepository, TransactionalSiteRepository
from app.db.models.site import Site
from app.db.models.siteicon import SiteIcon
from app.db.models.sitestatistic import SiteStatistic
from app.db.models.siteuserdata import SiteUserData
from app.db.session import SessionFactory, async_session_scope
from app.db.uow import SqlAlchemyAsyncUnitOfWork


def _transactional_repository() -> TransactionalSiteRepository:
    """按生产组合方式构造短 Session 站点仓储。"""
    return TransactionalSiteRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )


def test_transactional_repository_projects_frozen_site_snapshot(db) -> None:
    """独立仓储应在 Session 内复制 JSON 并返回不可变站点快照。"""
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
        snapshot.note["new"] = True  # type: ignore[index]


@pytest.mark.asyncio
async def test_transactional_repository_projects_related_snapshots(db) -> None:
    """用户数据、图标与统计查询均不得把 ORM 对象带出 Session。"""
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


@pytest.mark.asyncio
async def test_session_repository_leaves_commit_and_rollback_to_caller(db) -> None:
    """请求级仓储仅暂存，外层回滚和提交应分别决定最终可见性。"""
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

    assert repository.get(snapshot.id).pri == 8  # type: ignore[union-attr]
