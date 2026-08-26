"""站点 Chain 端口的显式会话与事务适配器。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, List, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.oper.site import SiteOper
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork

T = TypeVar("T")


class TransactionalSiteRepository:
    """为同步 Chain 站点端口和异步健康统计提供短生命周期会话。"""

    def __init__(
        self,
        *,
        sync_session: Callable[[], Session],
        async_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        """保存同步会话工厂和异步会话上下文工厂。"""
        self._sync_session = sync_session
        self._async_session = async_session

    def _read(self, operation: Callable[[SiteOper], T]) -> T:
        """在独立同步会话中执行只读站点操作。"""
        with self._sync_session() as session:
            return operation(SiteOper(db=session))

    def _write(self, operation: Callable[[SiteOper], T]) -> T:
        """在独立同步 UoW 中执行站点写操作。"""
        with self._sync_session() as session:
            session.expire_on_commit = False
            unit_of_work = SqlAlchemyUnitOfWork(session)
            try:
                result = operation(SiteOper(db=session))
                unit_of_work.commit()
                return result
            except Exception:
                unit_of_work.rollback()
                raise

    async def _async_write(
        self,
        operation: Callable[[SiteOper], Awaitable[T]],
    ) -> T:
        """在独立异步 UoW 中执行站点写操作。"""
        async with self._async_session() as session:
            session.sync_session.expire_on_commit = False
            unit_of_work = SqlAlchemyAsyncUnitOfWork(session)
            try:
                result = await operation(SiteOper(db=session))
                await unit_of_work.commit()
                return result
            except Exception:
                await unit_of_work.rollback()
                raise

    async def _async_read(self, operation: Callable[[SiteOper], Awaitable[T]]) -> T:
        """在独立异步会话中执行只读站点操作。"""
        async with self._async_session() as session:
            return await operation(SiteOper(db=session))

    def add(self, **kwargs: Any) -> tuple[bool, str]:
        """新增站点并提交事务。"""
        return self._write(lambda repository: repository.add(**kwargs))

    def get(self, site_id: int) -> Any:
        """按 ID 查询站点。"""
        return self._read(lambda repository: repository.get(site_id))

    def get_by_domain(self, domain: str) -> Any:
        """按域名查询站点。"""
        return self._read(lambda repository: repository.get_by_domain(domain))

    def get_domains_by_ids(self, ids: List[int]) -> List[str | None]:
        """查询一组站点 ID 对应的域名。"""
        return self._read(lambda repository: repository.get_domains_by_ids(ids))

    def list(self) -> List[Any]:
        """查询全部站点。"""
        return self._read(lambda repository: repository.list())

    def list_order_by_pri(self) -> List[Any]:
        """同步按优先级查询站点。"""
        return self._read(lambda repository: repository.list_order_by_pri())

    def get_userdata_latest(self) -> List[Any]:
        """同步查询各站点最新用户数据。"""
        return self._read(lambda repository: repository.get_userdata_latest())

    async def async_get(self, site_id: int) -> Any:
        """异步按 ID 查询站点。"""
        return await self._async_read(lambda repository: repository.async_get(site_id))

    async def async_get_by_domain(self, domain: str) -> Any:
        """异步按域名查询站点。"""
        return await self._async_read(
            lambda repository: repository.async_get_by_domain(domain)
        )

    async def async_get_by_name(self, name: str) -> Any:
        """异步按名称查询站点。"""
        return await self._async_read(
            lambda repository: repository.async_get_by_name(name)
        )

    async def async_list(self) -> List[Any]:
        """异步查询全部站点。"""
        return await self._async_read(lambda repository: repository.async_list())

    async def async_list_order_by_pri(self) -> List[Any]:
        """异步按优先级查询站点。"""
        return await self._async_read(
            lambda repository: repository.async_list_order_by_pri()
        )

    async def async_update(self, site_id: int, payload: dict[str, Any]) -> Any:
        """异步更新站点并提交事务。"""
        return await self._async_write(
            lambda repository: repository.async_update(site_id, payload)
        )

    async def async_get_userdata_by_domain(
        self,
        domain: str,
        workdate: str | None = None,
    ) -> List[Any]:
        """异步查询站点用户数据。"""
        return await self._async_read(
            lambda repository: repository.async_get_userdata_by_domain(domain, workdate)
        )

    async def async_get_userdata_latest(self) -> List[Any]:
        """异步查询各站点最新用户数据。"""
        return await self._async_read(
            lambda repository: repository.async_get_userdata_latest()
        )

    async def async_get_icon_by_domain(self, domain: str) -> Any:
        """异步按域名查询站点图标。"""
        return await self._async_read(
            lambda repository: repository.async_get_icon_by_domain(domain)
        )

    async def async_get_statistic_by_domain(self, domain: str) -> Any:
        """异步按域名查询站点统计。"""
        return await self._async_read(
            lambda repository: repository.async_get_statistic_by_domain(domain)
        )

    async def async_list_statistics(self) -> List[Any]:
        """异步查询全部站点统计。"""
        return await self._async_read(
            lambda repository: repository.async_list_statistics()
        )

    def update(self, site_id: int, payload: dict[str, Any]) -> Any:
        """更新站点并提交事务。"""
        return self._write(lambda repository: repository.update(site_id, payload))

    def update_cookie(self, domain: str, cookies: str) -> tuple[bool, str]:
        """更新站点 Cookie 并提交事务。"""
        return self._write(
            lambda repository: repository.update_cookie(domain, cookies)
        )

    def update_rss(self, domain: str, rss: str) -> tuple[bool, str]:
        """更新站点 RSS 地址并提交事务。"""
        return self._write(lambda repository: repository.update_rss(domain, rss))

    def update_userdata(
        self,
        domain: str,
        name: str,
        payload: dict[str, Any],
    ) -> tuple[bool, str]:
        """更新站点用户数据并提交事务。"""
        return self._write(
            lambda repository: repository.update_userdata(domain, name, payload)
        )

    def update_icon(
        self,
        name: str,
        domain: str,
        icon_url: str,
        icon_base64: str,
    ) -> bool:
        """更新站点图标并提交事务。"""
        return self._write(
            lambda repository: repository.update_icon(
                name,
                domain,
                icon_url,
                icon_base64,
            )
        )

    def success(self, domain: str, seconds: int | None = None) -> Any:
        """记录站点访问成功并提交事务。"""
        return self._write(lambda repository: repository.success(domain, seconds))

    def fail(self, domain: str) -> Any:
        """记录站点访问失败并提交事务。"""
        return self._write(lambda repository: repository.fail(domain))

    async def async_success(self, domain: str, seconds: int | None = None) -> Any:
        """异步记录站点访问成功并提交事务。"""
        return await self._async_write(
            lambda repository: repository.async_success(domain, seconds)
        )

    async def async_fail(self, domain: str) -> Any:
        """异步记录站点访问失败并提交事务。"""
        return await self._async_write(
            lambda repository: repository.async_fail(domain)
        )
