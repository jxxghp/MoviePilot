"""站点持久化 Port 的 SQLAlchemy 会话适配器。"""

from __future__ import annotations

import builtins
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Optional, TypeVar, cast

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.site.contract import (
    SiteIconSnapshot,
    SiteMutation,
    SitePriorityMutation,
    SiteSnapshot,
    SiteStatisticSnapshot,
    SiteUserDataMutation,
    SiteUserDataSnapshot,
    SiteWriteResult,
)
from app.db.models.site import Site
from app.db.models.siteicon import SiteIcon
from app.db.models.sitestatistic import SiteStatistic
from app.db.models.siteuserdata import SiteUserData
from app.db.oper.site import SiteOper
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.schemas.common import JsonData

T = TypeVar("T")


def _project_site(record: Site) -> SiteSnapshot:
    """在 ORM 所属 Session 内复制完整站点配置。"""
    return SiteSnapshot(
        id=record.id,
        name=record.name,
        url=record.url,
        domain=record.domain,
        pri=record.pri,
        rss=record.rss,
        cookie=record.cookie,
        ua=record.ua,
        apikey=record.apikey,
        token=record.token,
        proxy=record.proxy,
        filter=record.filter,
        render=record.render,
        public=record.public,
        note=cast(Optional[JsonData], record.note),
        limit_interval=record.limit_interval,
        limit_count=record.limit_count,
        limit_seconds=record.limit_seconds,
        timeout=record.timeout,
        is_active=record.is_active,
        lst_mod_date=record.lst_mod_date,
        downloader=record.downloader,
    )


def _project_userdata(record: SiteUserData) -> SiteUserDataSnapshot:
    """在 ORM 所属 Session 内复制站点用户数据。"""
    return SiteUserDataSnapshot(
        id=record.id,
        domain=record.domain,
        name=record.name,
        username=record.username,
        userid=record.userid,
        user_level=record.user_level,
        join_at=record.join_at,
        bonus=record.bonus,
        upload=record.upload,
        download=record.download,
        ratio=record.ratio,
        seeding=record.seeding,
        leeching=record.leeching,
        seeding_size=record.seeding_size,
        leeching_size=record.leeching_size,
        seeding_info=cast(Optional[JsonData], record.seeding_info),
        message_unread=record.message_unread,
        message_unread_contents=cast(
            Optional[JsonData],
            record.message_unread_contents,
        ),
        err_msg=record.err_msg,
        updated_day=record.updated_day,
        updated_time=record.updated_time,
    )


def _project_icon(record: SiteIcon) -> SiteIconSnapshot:
    """在 ORM 所属 Session 内复制站点图标。"""
    return SiteIconSnapshot(
        id=record.id,
        name=record.name,
        url=record.url,
        domain=record.domain,
        base64=record.base64,
    )


def _project_statistic(record: SiteStatistic) -> SiteStatisticSnapshot:
    """在 ORM 所属 Session 内复制站点健康统计。"""
    return SiteStatisticSnapshot(
        id=record.id,
        domain=record.domain,
        success=record.success,
        fail=record.fail,
        seconds=record.seconds,
        lst_state=record.lst_state,
        lst_mod_date=record.lst_mod_date,
        note=cast(Optional[JsonData], record.note),
    )


class TransactionalSiteRepository:
    """通过独立短 Session 实现完整站点查询与写入 Port。"""

    def __init__(
        self,
        *,
        sync_session: Callable[[], Session],
        async_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        """保存同步和异步短 Session 工厂。"""
        self._sync_session = sync_session
        self._async_session = async_session

    def _read(self, operation: Callable[[SiteOper], T]) -> T:
        """在独立同步 Session 中执行并完成 DTO 投影。"""
        with self._sync_session() as session:
            return operation(SiteOper(db=session))

    def _write(self, operation: Callable[[SiteOper], T]) -> T:
        """在独立同步 UoW 中执行站点写操作。"""
        with self._sync_session() as session:
            unit_of_work = SqlAlchemyUnitOfWork(session)
            try:
                result = operation(SiteOper(db=session))
                unit_of_work.commit()
                return result
            except Exception:
                unit_of_work.rollback()
                raise

    async def _async_read(
        self,
        operation: Callable[[SiteOper], Awaitable[T]],
    ) -> T:
        """在独立异步 Session 中执行并完成 DTO 投影。"""
        async with self._async_session() as session:
            return await operation(SiteOper(db=session))

    async def _async_write(
        self,
        operation: Callable[[SiteOper], Awaitable[T]],
    ) -> T:
        """在独立异步 UoW 中执行站点写操作。"""
        async with self._async_session() as session:
            unit_of_work = SqlAlchemyAsyncUnitOfWork(session)
            try:
                result = await operation(SiteOper(db=session))
                await unit_of_work.commit()
                return result
            except Exception:
                await unit_of_work.rollback()
                raise

    def get(self, site_id: int) -> Optional[SiteSnapshot]:
        """同步按主键读取站点快照。"""
        return self._read(
            lambda repository: _project_site(record) if (record := repository.get(site_id)) is not None else None
        )

    def get_by_domain(self, domain: str) -> Optional[SiteSnapshot]:
        """同步按域名读取站点快照。"""
        return self._read(
            lambda repository: (
                _project_site(record) if (record := repository.get_by_domain(domain)) is not None else None
            )
        )

    def get_domains_by_ids(
        self,
        ids: builtins.list[int],
    ) -> builtins.list[str]:
        """同步读取一组站点主键对应的非空域名。"""
        return self._read(
            lambda repository: [domain for domain in repository.get_domains_by_ids(ids) if domain is not None]
        )

    def list(self) -> builtins.list[SiteSnapshot]:
        """同步读取全部站点快照。"""
        return self._read(lambda repository: [_project_site(item) for item in repository.list()])

    def list_order_by_pri(self) -> builtins.list[SiteSnapshot]:
        """同步按优先级读取站点快照。"""
        return self._read(lambda repository: [_project_site(item) for item in repository.list_order_by_pri()])

    def list_active(self) -> builtins.list[SiteSnapshot]:
        """同步读取已启用站点快照。"""
        return self._read(lambda repository: [_project_site(item) for item in repository.list_active()])

    def get_userdata_latest(self) -> builtins.list[SiteUserDataSnapshot]:
        """同步读取各站点最新用户数据快照。"""
        return self._read(lambda repository: [_project_userdata(item) for item in repository.get_userdata_latest()])

    async def async_get(self, site_id: int) -> Optional[SiteSnapshot]:
        """异步按主键读取站点快照。"""

        async def operation(repository: SiteOper) -> Optional[SiteSnapshot]:
            """读取并在当前异步 Session 中投影站点。"""
            record = await repository.async_get(site_id)
            return _project_site(record) if record is not None else None

        return await self._async_read(operation)

    async def async_get_by_domain(self, domain: str) -> Optional[SiteSnapshot]:
        """异步按域名读取站点快照。"""

        async def operation(repository: SiteOper) -> Optional[SiteSnapshot]:
            """读取并在当前异步 Session 中投影站点。"""
            record = await repository.async_get_by_domain(domain)
            return _project_site(record) if record is not None else None

        return await self._async_read(operation)

    async def async_get_by_name(self, name: str) -> Optional[SiteSnapshot]:
        """异步按名称读取站点快照。"""

        async def operation(repository: SiteOper) -> Optional[SiteSnapshot]:
            """读取并在当前异步 Session 中投影站点。"""
            record = await repository.async_get_by_name(name)
            return _project_site(record) if record is not None else None

        return await self._async_read(operation)

    async def async_list(self) -> builtins.list[SiteSnapshot]:
        """异步读取全部站点快照。"""

        async def operation(
            repository: SiteOper,
        ) -> builtins.list[SiteSnapshot]:
            """读取并在当前异步 Session 中投影全部站点。"""
            return [_project_site(item) for item in await repository.async_list()]

        return await self._async_read(operation)

    async def async_list_order_by_pri(
        self,
        *,
        is_active: Optional[bool] = None,
        name: Optional[str] = None,
        site_ids: Optional[builtins.list[int]] = None,
        domains: Optional[builtins.list[str]] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SiteSnapshot]:
        """按筛选、优先级和可选分页窗口异步读取站点快照。"""

        async def operation(
            repository: SiteOper,
        ) -> builtins.list[SiteSnapshot]:
            """读取并在当前异步 Session 中投影筛选后的站点。"""
            records = await repository.async_list_order_by_pri(
                is_active=is_active,
                name=name,
                site_ids=site_ids,
                domains=domains,
                page=page,
                count=count,
            )
            return [_project_site(item) for item in records]

        return await self._async_read(operation)

    async def async_count_sites(
        self,
        *,
        is_active: Optional[bool] = None,
        name: Optional[str] = None,
        site_ids: Optional[builtins.list[int]] = None,
        domains: Optional[builtins.list[str]] = None,
    ) -> int:
        """按与站点列表一致的筛选条件异步统计数量。"""
        return await self._async_read(
            lambda repository: repository.async_count_sites(
                is_active=is_active,
                name=name,
                site_ids=site_ids,
                domains=domains,
            )
        )

    async def async_list_active(self) -> builtins.list[SiteSnapshot]:
        """异步读取已启用站点快照。"""

        async def operation(
            repository: SiteOper,
        ) -> builtins.list[SiteSnapshot]:
            """读取并在当前异步 Session 中投影已启用站点。"""
            records = await repository.async_list_active()
            return [_project_site(item) for item in records]

        return await self._async_read(operation)

    async def async_get_userdata_by_domain(
        self,
        domain: str,
        workdate: Optional[str] = None,
        *,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SiteUserDataSnapshot]:
        """按可选分页窗口异步读取指定域名和日期的用户数据快照。"""

        async def operation(
            repository: SiteOper,
        ) -> builtins.list[SiteUserDataSnapshot]:
            """读取并在当前异步 Session 中投影用户数据。"""
            records = await repository.async_get_userdata_by_domain(
                domain,
                workdate,
                page=page,
                count=count,
            )
            return [_project_userdata(item) for item in records]

        return await self._async_read(operation)

    async def async_count_userdata_by_domain(
        self,
        domain: str,
        workdate: Optional[str] = None,
    ) -> int:
        """异步统计指定域名和日期的用户数据数量。"""
        return await self._async_read(
            lambda repository: repository.async_count_userdata_by_domain(
                domain,
                workdate,
            )
        )

    async def async_get_userdata_latest(
        self,
        *,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SiteUserDataSnapshot]:
        """按可选分页窗口异步读取各站点最新用户数据快照。"""

        async def operation(
            repository: SiteOper,
        ) -> builtins.list[SiteUserDataSnapshot]:
            """读取并在当前异步 Session 中投影最新用户数据。"""
            records = await repository.async_get_userdata_latest(
                page=page,
                count=count,
            )
            return [_project_userdata(item) for item in records]

        return await self._async_read(operation)

    async def async_count_userdata_latest(self) -> int:
        """异步统计各站点最新用户数据查询的结果数量。"""
        return await self._async_read(
            lambda repository: repository.async_count_userdata_latest()
        )

    async def async_get_icon_by_domain(
        self,
        domain: str,
    ) -> Optional[SiteIconSnapshot]:
        """异步按域名读取站点图标快照。"""

        async def operation(repository: SiteOper) -> Optional[SiteIconSnapshot]:
            """读取并在当前异步 Session 中投影图标。"""
            record = await repository.async_get_icon_by_domain(domain)
            return _project_icon(record) if record is not None else None

        return await self._async_read(operation)

    async def async_get_statistic_by_domain(
        self,
        domain: str,
    ) -> Optional[SiteStatisticSnapshot]:
        """异步按域名读取站点健康统计快照。"""

        async def operation(
            repository: SiteOper,
        ) -> Optional[SiteStatisticSnapshot]:
            """读取并在当前异步 Session 中投影统计。"""
            record = await repository.async_get_statistic_by_domain(domain)
            return _project_statistic(record) if record is not None else None

        return await self._async_read(operation)

    async def async_list_statistics(
        self,
        *,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SiteStatisticSnapshot]:
        """按可选分页窗口异步读取站点健康统计快照。"""

        async def operation(
            repository: SiteOper,
        ) -> builtins.list[SiteStatisticSnapshot]:
            """读取并在当前异步 Session 中投影全部统计。"""
            records = await repository.async_list_statistics(
                page=page,
                count=count,
            )
            return [_project_statistic(item) for item in records]

        return await self._async_read(operation)

    async def async_count_statistics(self) -> int:
        """异步统计站点健康统计记录数量。"""
        return await self._async_read(
            lambda repository: repository.async_count_statistics()
        )

    def add(self, mutation: SiteMutation) -> SiteWriteResult:
        """在独立同步事务中新增站点。"""
        return self._write(lambda repository: SiteWriteResult(*repository.add(**mutation.to_payload())))

    def update(
        self,
        site_id: int,
        mutation: SiteMutation,
    ) -> Optional[SiteSnapshot]:
        """在独立同步事务中更新并返回站点快照。"""
        return self._write(
            lambda repository: (
                _project_site(record)
                if (record := repository.update(site_id, mutation.to_payload())) is not None
                else None
            )
        )

    async def async_update(
        self,
        site_id: int,
        mutation: SiteMutation,
    ) -> Optional[SiteSnapshot]:
        """在独立异步事务中更新并返回站点快照。"""

        async def operation(repository: SiteOper) -> Optional[SiteSnapshot]:
            """更新并在当前异步 Session 中投影站点。"""
            record = await repository.async_update(
                site_id,
                mutation.to_payload(),
            )
            return _project_site(record) if record is not None else None

        return await self._async_write(operation)

    def update_cookie(self, domain: str, cookies: str) -> SiteWriteResult:
        """在独立同步事务中更新站点 Cookie。"""
        return self._write(lambda repository: SiteWriteResult(*repository.update_cookie(domain, cookies)))

    def update_rss(self, domain: str, rss: str) -> SiteWriteResult:
        """在独立同步事务中更新站点 RSS。"""
        return self._write(lambda repository: SiteWriteResult(*repository.update_rss(domain, rss)))

    def update_userdata(
        self,
        domain: str,
        name: str,
        mutation: SiteUserDataMutation,
    ) -> SiteWriteResult:
        """在独立同步事务中写入站点用户数据。"""
        return self._write(
            lambda repository: SiteWriteResult(
                *repository.update_userdata(
                    domain,
                    name,
                    mutation.to_payload(),
                )
            )
        )

    def update_icon(
        self,
        name: str,
        domain: str,
        icon_url: str,
        icon_base64: str,
    ) -> bool:
        """在独立同步事务中写入站点图标。"""
        return self._write(
            lambda repository: repository.update_icon(
                name,
                domain,
                icon_url,
                icon_base64,
            )
        )

    def success(self, domain: str, seconds: Optional[int] = None) -> None:
        """在独立同步事务中记录站点访问成功。"""
        self._write(lambda repository: repository.success(domain, seconds))

    def fail(self, domain: str) -> None:
        """在独立同步事务中记录站点访问失败。"""
        self._write(lambda repository: repository.fail(domain))

    async def async_success(
        self,
        domain: str,
        seconds: Optional[int] = None,
    ) -> None:
        """在独立异步事务中记录站点访问成功。"""

        async def operation(repository: SiteOper) -> None:
            """在当前异步事务内登记站点成功。"""
            await repository.async_success(domain, seconds)

        await self._async_write(operation)

    async def async_fail(self, domain: str) -> None:
        """在独立异步事务中记录站点访问失败。"""

        async def operation(repository: SiteOper) -> None:
            """在当前异步事务内登记站点失败。"""
            await repository.async_fail(domain)

        await self._async_write(operation)


class SessionSiteRepository:
    """复用请求 AsyncSession，负责查询投影和暂存但不提交。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由请求依赖持有的 AsyncSession。"""
        self._repository = SiteOper(db=session)

    async def get_by_id(self, site_id: int) -> Optional[SiteSnapshot]:
        """在请求 Session 中按主键读取站点快照。"""
        record = await self._repository.get_by_id(site_id)
        return _project_site(record) if record is not None else None

    async def async_get(self, site_id: int) -> Optional[SiteSnapshot]:
        """为异步查询 Port 按主键读取站点快照。"""
        record = await self._repository.async_get(site_id)
        return _project_site(record) if record is not None else None

    async def async_get_by_domain(self, domain: str) -> Optional[SiteSnapshot]:
        """在请求 Session 中按域名读取站点快照。"""
        record = await self._repository.async_get_by_domain(domain)
        return _project_site(record) if record is not None else None

    async def async_get_by_name(self, name: str) -> Optional[SiteSnapshot]:
        """在请求 Session 中按名称读取站点快照。"""
        record = await self._repository.async_get_by_name(name)
        return _project_site(record) if record is not None else None

    async def async_list(self) -> builtins.list[SiteSnapshot]:
        """在请求 Session 中读取全部站点快照。"""
        records = await self._repository.async_list()
        return [_project_site(item) for item in records]

    async def async_list_order_by_pri(
        self,
        *,
        is_active: Optional[bool] = None,
        name: Optional[str] = None,
        site_ids: Optional[builtins.list[int]] = None,
        domains: Optional[builtins.list[str]] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SiteSnapshot]:
        """在请求 Session 中按筛选、优先级和分页窗口读取站点。"""
        records = await self._repository.async_list_order_by_pri(
            is_active=is_active,
            name=name,
            site_ids=site_ids,
            domains=domains,
            page=page,
            count=count,
        )
        return [_project_site(item) for item in records]

    async def async_count_sites(
        self,
        *,
        is_active: Optional[bool] = None,
        name: Optional[str] = None,
        site_ids: Optional[builtins.list[int]] = None,
        domains: Optional[builtins.list[str]] = None,
    ) -> int:
        """在请求 Session 中按列表筛选统计站点数量。"""
        return await self._repository.async_count_sites(
            is_active=is_active,
            name=name,
            site_ids=site_ids,
            domains=domains,
        )

    async def async_list_active(self) -> builtins.list[SiteSnapshot]:
        """在请求 Session 中读取已启用站点快照。"""
        records = await self._repository.async_list_active()
        return [_project_site(item) for item in records]

    async def async_get_userdata_by_domain(
        self,
        domain: str,
        workdate: Optional[str] = None,
        *,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SiteUserDataSnapshot]:
        """在请求 Session 中按可选分页窗口读取站点用户数据快照。"""
        records = await self._repository.async_get_userdata_by_domain(
            domain,
            workdate,
            page=page,
            count=count,
        )
        return [_project_userdata(item) for item in records]

    async def async_count_userdata_by_domain(
        self,
        domain: str,
        workdate: Optional[str] = None,
    ) -> int:
        """在请求 Session 中统计指定站点和日期的用户数据数量。"""
        return await self._repository.async_count_userdata_by_domain(
            domain,
            workdate,
        )

    async def async_get_userdata_latest(
        self,
        *,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SiteUserDataSnapshot]:
        """在请求 Session 中按可选分页窗口读取最新用户数据快照。"""
        records = await self._repository.async_get_userdata_latest(
            page=page,
            count=count,
        )
        return [_project_userdata(item) for item in records]

    async def async_count_userdata_latest(self) -> int:
        """在请求 Session 中统计各站点最新用户数据结果数量。"""
        return await self._repository.async_count_userdata_latest()

    async def async_get_icon_by_domain(
        self,
        domain: str,
    ) -> Optional[SiteIconSnapshot]:
        """在请求 Session 中按域名读取站点图标快照。"""
        record = await self._repository.async_get_icon_by_domain(domain)
        return _project_icon(record) if record is not None else None

    async def async_get_statistic_by_domain(
        self,
        domain: str,
    ) -> Optional[SiteStatisticSnapshot]:
        """在请求 Session 中按域名读取站点统计快照。"""
        record = await self._repository.async_get_statistic_by_domain(domain)
        return _project_statistic(record) if record is not None else None

    async def async_list_statistics(
        self,
        *,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SiteStatisticSnapshot]:
        """在请求 Session 中按可选分页窗口读取站点统计快照。"""
        records = await self._repository.async_list_statistics(
            page=page,
            count=count,
        )
        return [_project_statistic(item) for item in records]

    async def async_count_statistics(self) -> int:
        """在请求 Session 中统计站点健康统计记录数量。"""
        return await self._repository.async_count_statistics()

    async def stage_create(self, mutation: SiteMutation) -> None:
        """在请求事务中暂存新增站点。"""
        await self._repository.stage_create(mutation.to_payload())

    async def stage_update(
        self,
        site_id: int,
        mutation: SiteMutation,
    ) -> bool:
        """在请求事务中暂存站点更新并返回目标是否存在。"""
        return await self._repository.stage_update(
            site_id,
            mutation.to_payload(),
        )

    async def stage_delete(self, site_id: int) -> None:
        """在请求事务中暂存删除站点。"""
        await self._repository.stage_delete(site_id)

    async def stage_priorities(
        self,
        priorities: tuple[SitePriorityMutation, ...],
    ) -> None:
        """在请求事务中暂存一组站点优先级更新。"""
        await self._repository.stage_priorities([{"id": item.site_id, "pri": item.priority} for item in priorities])

    async def stage_reset(self) -> None:
        """在请求事务中暂存清空全部站点。"""
        await self._repository.stage_reset()
