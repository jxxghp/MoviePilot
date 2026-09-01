from datetime import datetime
from typing import Any, List, Mapping, Optional, Tuple

from sqlalchemy import delete as sqlalchemy_delete
from sqlalchemy import false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.site import Site
from app.db.models.siteicon import SiteIcon
from app.db.models.sitestatistic import SiteStatistic
from app.db.models.siteuserdata import SiteUserData
from app.db.oper.query import literal_contains


async def _async_first(session: AsyncSession, statement: Any) -> Optional[Site]:
    """执行异步站点查询并返回首条记录。"""
    result = await session.execute(statement)
    return result.scalars().first()


async def _async_all(session: AsyncSession, statement: Any) -> list[Any]:
    """执行异步列表查询并返回稳定 ORM 行。"""
    result = await session.execute(statement)
    return list(result.scalars().all())


async def _async_scalar(session: AsyncSession, statement: Any) -> int:
    """执行异步计数语句并返回整数。"""
    result = await session.execute(statement)
    return int(result.scalar_one() or 0)


def _apply_page(statement: Any, page: Optional[int], count: Optional[int]) -> Any:
    """仅在分页窗口完整时向 SQL 语句追加 LIMIT/OFFSET。"""
    if page is None or count is None:
        return statement
    return statement.offset((page - 1) * count).limit(count)


def _site_conditions(
    *,
    is_active: Optional[bool] = None,
    name: Optional[str] = None,
    site_ids: Optional[list[int]] = None,
    domains: Optional[list[str]] = None,
) -> list[Any]:
    """构造站点列表与计数共享的数据库筛选条件。"""
    conditions: list[Any] = []
    if is_active is not None:
        conditions.append(Site.is_active.is_(is_active))
    if name:
        conditions.append(literal_contains(Site.name, name))
    if site_ids is not None or domains is not None:
        identity_conditions: list[Any] = []
        if site_ids:
            identity_conditions.append(Site.id.in_(site_ids))
        if domains:
            identity_conditions.append(Site.domain.in_(domains))
        conditions.append(
            or_(*identity_conditions) if identity_conditions else false()
        )
    return conditions


def _userdata_conditions(
    domain: str,
    workdate: Optional[str],
) -> list[Any]:
    """构造站点用户数据列表与计数共享的筛选条件。"""
    conditions = [SiteUserData.domain == domain]
    if workdate:
        conditions.append(SiteUserData.updated_day == workdate)
    return conditions


def _latest_userdata_subquery() -> Any:
    """构造各站点最新有效数据日期的共享子查询。"""
    return (
        select(
            SiteUserData.domain,
            func.max(SiteUserData.updated_day).label("latest_update_day"),
        )
        .where(or_(SiteUserData.err_msg.is_(None), SiteUserData.err_msg == ""))
        .group_by(SiteUserData.domain)
        .subquery()
    )


def _latest_userdata_join(statement: Any, subquery: Any) -> Any:
    """把最新用户数据日期子查询连接到目标查询语句。"""
    return statement.join(
        subquery,
        (SiteUserData.domain == subquery.c.domain)
        & (SiteUserData.updated_day == subquery.c.latest_update_day),
    )


class SiteOper(DbOper):
    """
    站点管理
    """

    def add(self, **kwargs) -> Tuple[bool, str]:
        """
        新增站点
        """
        site = Site(**kwargs)
        if not self.get_by_domain(kwargs.get("domain")):
            self._stage_create(site)
            return True, "新增站点成功"
        return False, "站点已存在"

    def get(self, sid: int) -> Optional[Site]:
        """
        查询单个站点
        """
        return self._execute_sync_query(
            lambda session: session.execute(
                select(Site).where(Site.id == sid)
            ).scalars().first()
        )

    async def async_get(self, sid: int) -> Optional[Site]:
        """
        异步查询单个站点
        """
        return await self._execute_async_query(
            lambda session: _async_first(
                session,
                select(Site).where(Site.id == sid),
            )
        )

    async def get_by_id(self, site_id: int) -> Optional[Site]:
        """读取站点写用例需要的目标站点。"""
        return await self.async_get(site_id)

    async def stage_create(self, payload: Mapping[str, Any]) -> None:
        """暂存新增站点，不由仓储自行提交。"""
        values = dict(payload)
        values.pop("id", None)
        self._db.add(Site(**values))

    async def stage_update(
            self,
            site_id: int,
            payload: Mapping[str, Any],
    ) -> bool:
        """暂存站点字段更新，事务由调用方统一提交。"""
        site = await self.async_get(site_id)
        if not site:
            return False
        for key, value in payload.items():
            if key != "id":
                setattr(site, key, value)
        return True

    async def stage_delete(self, site_id: int) -> None:
        """暂存站点删除，由请求级 UnitOfWork 统一提交。"""
        await self._db.execute(
            sqlalchemy_delete(Site).where(Site.id == site_id)
        )

    async def stage_priorities(self, priorities: list[dict]) -> None:
        """暂存批量优先级更新，避免逐行独立提交。"""
        for priority in priorities:
            site_id = priority.get("id")
            site = await self.async_get(site_id) if site_id else None
            if site:
                site.pri = priority.get("pri")

    def list(self) -> List[Site]:
        """
        获取站点列表
        """
        return self._execute_sync_query(
            lambda session: list(session.execute(select(Site)).scalars().all())
        )

    async def async_list(self) -> List[Site]:
        """
        异步获取站点列表
        """
        return await self._execute_async_query(
            lambda session: _async_all(session, select(Site))
        )

    async def async_list_order_by_pri(
        self,
        *,
        is_active: Optional[bool] = None,
        name: Optional[str] = None,
        site_ids: Optional[List[int]] = None,
        domains: Optional[List[str]] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> List[Site]:
        """按筛选、优先级和可选分页窗口异步获取站点。"""
        statement = select(Site).where(
            *_site_conditions(
                is_active=is_active,
                name=name,
                site_ids=site_ids,
                domains=domains,
            )
        ).order_by(Site.pri, Site.id)
        return await self._execute_async_query(
            lambda session: _async_all(
                session,
                _apply_page(statement, page, count),
            )
        )

    async def async_count_sites(
        self,
        *,
        is_active: Optional[bool] = None,
        name: Optional[str] = None,
        site_ids: Optional[List[int]] = None,
        domains: Optional[List[str]] = None,
    ) -> int:
        """按与站点列表一致的筛选条件异步统计数量。"""
        statement = select(func.count()).select_from(Site).where(
            *_site_conditions(
                is_active=is_active,
                name=name,
                site_ids=site_ids,
                domains=domains,
            )
        )
        return await self._execute_async_query(
            lambda session: _async_scalar(session, statement)
        )

    def list_order_by_pri(self) -> List[Site]:
        """
        获取站点列表
        """
        return self._execute_sync_query(
            lambda session: list(
                session.execute(select(Site).order_by(Site.pri)).scalars().all()
            )
        )

    def list_active(self) -> List[Site]:
        """
        按状态获取站点列表
        """
        return self._execute_sync_query(
            lambda session: list(
                session.execute(
                    select(Site).where(Site.is_active.is_(True))
                ).scalars().all()
            )
        )

    async def async_list_active(self) -> List[Site]:
        """
        异步按状态获取站点列表
        """
        return await self._execute_async_query(
            lambda session: _async_all(
                session,
                select(Site).where(Site.is_active.is_(True)),
            )
        )

    def delete(self, sid: int):
        """
        删除站点
        """
        self._stage_delete(Site, sid)

    def reset(self) -> None:
        """清空站点表；兼容入口的事务由组合根统一持有。"""
        self._execute_sync_write(Site.reset)

    async def stage_reset(self) -> None:
        """暂存清空站点表，由应用事务统一提交。"""
        await self._db.execute(sqlalchemy_delete(Site))

    def update(self, sid: int, payload: dict) -> Optional[Site]:
        """
        更新站点
        """
        site = self.get(sid)
        if not site:
            return None
        self._stage_update(site, payload)
        return site

    async def async_update(self, sid: int, payload: dict) -> Optional[Site]:
        """
        异步更新站点。
        """
        site = await self.async_get(sid)
        if site:
            await self._stage_async_update(site, payload)
        return site

    def get_by_domain(self, domain: str) -> Optional[Site]:
        """
        按域名获取站点
        """
        return self._execute_sync_query(
            lambda session: session.execute(
                select(Site).where(Site.domain == domain)
            ).scalars().first()
        )

    async def async_get_by_domain(self, domain: str) -> Optional[Site]:
        """
        异步按域名获取站点
        """
        return await self._execute_async_query(
            lambda session: _async_first(
                session,
                select(Site).where(Site.domain == domain),
            )
        )

    async def async_get_by_name(self, name: str) -> Optional[Site]:
        """
        异步按名称获取站点
        """
        return await self._execute_async_query(
            lambda session: _async_first(
                session,
                select(Site).where(Site.name == name),
            )
        )

    def get_domains_by_ids(self, ids: List[int]) -> List[Optional[str]]:
        """
        按ID获取站点域名
        """
        if not ids:
            return []
        return self._execute_sync_query(
            lambda session: list(
                session.execute(
                    select(Site.domain).where(Site.id.in_(ids))
                ).scalars().all()
            )
        )

    def exists(self, domain: str) -> bool:
        """
        判断站点是否存在
        """
        return self.get_by_domain(domain) is not None

    def update_cookie(self, domain: str, cookies: str) -> Tuple[bool, str]:
        """
        更新站点Cookie
        """
        site = self.get_by_domain(domain)
        if not site:
            return False, "站点不存在"
        self._stage_update(site, {
            "cookie": cookies
        })
        return True, "更新站点Cookie成功"

    def update_rss(self, domain: str, rss: str) -> Tuple[bool, str]:
        """
        更新站点rss
        """
        site = self.get_by_domain(domain)
        if not site:
            return False, "站点不存在"
        self._stage_update(site, {
            "rss": rss
        })
        return True, "更新站点RSS地址成功"

    def update_userdata(self, domain: str, name: str, payload: dict) -> Tuple[bool, str]:
        """
        更新站点用户数据
        """
        # 当前系统日期
        current_day = datetime.now().strftime('%Y-%m-%d')
        current_time = datetime.now().strftime('%H:%M:%S')
        payload.update({
            "domain": domain,
            "name": name,
            "updated_day": current_day,
            "updated_time": current_time,
            "err_msg": payload.get("err_msg") or ""
        })
        # 按站点+天判断是否存在数据
        siteuserdatas = self._execute_sync_query(
            lambda session: SiteUserData.get_by_domain(
                session,
                domain=domain,
                workdate=current_day,
            )
        )
        if siteuserdatas:
            # 存在则更新
            if not payload.get("err_msg"):
                self._stage_update(siteuserdatas[0], payload)
        else:
            # 不存在则插入
            self._stage_create(SiteUserData(**payload))
        return True, "更新站点用户数据成功"

    def get_userdata(self) -> List[SiteUserData]:
        """
        获取站点用户数据
        """
        return self._execute_sync_query(
            lambda session: SiteUserData.list(session)
        )

    def get_userdata_by_domain(self, domain: str, workdate: Optional[str] = None) -> List[SiteUserData]:
        """
        获取站点用户数据
        """
        return self._execute_sync_query(
            lambda session: SiteUserData.get_by_domain(
                session,
                domain=domain,
                workdate=workdate,
            )
        )

    async def async_get_userdata_by_domain(
        self,
        domain: str,
        workdate: Optional[str] = None,
        *,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> List[SiteUserData]:
        """按可选分页窗口异步获取站点用户数据。"""
        statement = select(SiteUserData).where(
            *_userdata_conditions(domain, workdate)
        ).order_by(
            SiteUserData.updated_day.desc(),
            SiteUserData.updated_time.desc(),
            SiteUserData.id.desc(),
        )
        return await self._execute_async_query(
            lambda session: _async_all(
                session,
                _apply_page(statement, page, count),
            )
        )

    async def async_count_userdata_by_domain(
        self,
        domain: str,
        workdate: Optional[str] = None,
    ) -> int:
        """异步统计指定站点和日期的用户数据数量。"""
        statement = select(func.count()).select_from(SiteUserData).where(
            *_userdata_conditions(domain, workdate)
        )
        return await self._execute_async_query(
            lambda session: _async_scalar(session, statement)
        )

    async def async_get_userdata_latest(
        self,
        *,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> List[SiteUserData]:
        """按可选分页窗口异步获取各站点最新用户数据。"""
        subquery = _latest_userdata_subquery()
        statement = _latest_userdata_join(select(SiteUserData), subquery).order_by(
            SiteUserData.updated_time.desc(),
            SiteUserData.id.desc(),
        )
        return await self._execute_async_query(
            lambda session: _async_all(
                session,
                _apply_page(statement, page, count),
            )
        )

    async def async_count_userdata_latest(self) -> int:
        """异步统计各站点最新用户数据查询的结果数量。"""
        subquery = _latest_userdata_subquery()
        statement = _latest_userdata_join(
            select(func.count()).select_from(SiteUserData),
            subquery,
        )
        return await self._execute_async_query(
            lambda session: _async_scalar(session, statement)
        )

    async def async_get_icon_by_domain(self, domain: str) -> Optional[SiteIcon]:
        """异步按域名获取站点图标。"""
        return await self._execute_async_query(
            lambda session: SiteIcon.async_get_by_domain(session, domain)
        )

    async def async_get_statistic_by_domain(
        self,
        domain: str,
    ) -> Optional[SiteStatistic]:
        """异步按域名获取站点统计。"""
        return await self._execute_async_query(
            lambda session: SiteStatistic.async_get_by_domain(session, domain)
        )

    async def async_list_statistics(
        self,
        *,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> List[SiteStatistic]:
        """按可选分页窗口异步获取站点统计。"""
        statement = select(SiteStatistic).order_by(SiteStatistic.id)
        return await self._execute_async_query(
            lambda session: _async_all(
                session,
                _apply_page(statement, page, count),
            )
        )

    async def async_count_statistics(self) -> int:
        """异步统计站点健康统计记录数量。"""
        statement = select(func.count()).select_from(SiteStatistic)
        return await self._execute_async_query(
            lambda session: _async_scalar(session, statement)
        )

    def get_userdata_by_date(self, date: str) -> List[SiteUserData]:
        """
        获取站点用户数据
        """
        return self._execute_sync_query(
            lambda session: SiteUserData.get_by_date(session, date)
        )

    def get_userdata_latest(self) -> List[SiteUserData]:
        """
        获取站点最新数据
        """
        return self._execute_sync_query(
            lambda session: SiteUserData.get_latest(session)
        )

    def get_icon_by_domain(self, domain: str) -> Optional[SiteIcon]:
        """
        按域名获取站点图标
        """
        return self._execute_sync_query(
            lambda session: SiteIcon.get_by_domain(session, domain)
        )

    def update_icon(self, name: str, domain: str, icon_url: str, icon_base64: str) -> bool:
        """
        更新站点图标
        """
        icon_base64 = f"data:image/ico;base64,{icon_base64}" if icon_base64 else ""

        def write(db: Session) -> None:
            """在同一同步事务中查询并更新站点图标。"""
            siteicon = SiteIcon.get_by_domain(db, domain)
            if not siteicon:
                db.add(SiteIcon(
                    name=name,
                    domain=domain,
                    url=icon_url,
                    base64=icon_base64,
                ))
            elif icon_base64:
                siteicon.url = icon_url
                siteicon.base64 = icon_base64

        self._execute_sync_write(write)
        return True

    def success(self, domain: str, seconds: Optional[int] = None):
        """
        站点访问成功
        """
        def write(db: Session) -> None:
            """在同一同步事务中读取并更新站点统计。"""
            lst_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sta = SiteStatistic.get_by_domain(db, domain)
            if sta:
                # 使用深复制确保 note 是全新的字典对象
                note = dict(sta.note) if sta.note else {}
                avg_seconds = None

                if seconds is not None:
                    note[lst_date] = seconds or 1
                    avg_times = len(note.keys())
                    if avg_times > 10:
                        note = dict(sorted(note.items(), key=lambda x: x[0], reverse=True)[:10])
                    avg_seconds = sum([v for v in note.values()]) // avg_times

                for key, value in {
                    "success": sta.success + 1,
                    "seconds": avg_seconds or sta.seconds,
                    "lst_state": 0,
                    "lst_mod_date": lst_date,
                    "note": note,
                }.items():
                    setattr(sta, key, value)
            else:
                note = {}
                if seconds is not None:
                    note = {lst_date: seconds or 1}
                db.add(SiteStatistic(
                    domain=domain,
                    success=1,
                    fail=0,
                    seconds=seconds or 1,
                    lst_state=0,
                    lst_mod_date=lst_date,
                    note=note,
                ))

        self._execute_sync_write(write)

    def fail(self, domain: str):
        """
        站点访问失败
        """
        def write(db: Session) -> None:
            """在同一同步事务中读取并更新站点失败统计。"""
            lst_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sta = SiteStatistic.get_by_domain(db, domain)
            if sta:
                sta.fail += 1
                sta.lst_state = 1
                sta.lst_mod_date = lst_date
            else:
                db.add(SiteStatistic(
                    domain=domain,
                    success=0,
                    fail=1,
                    lst_state=1,
                    lst_mod_date=lst_date,
                ))

        self._execute_sync_write(write)

    async def async_success(self, domain: str, seconds: Optional[int] = None):
        """
        异步站点访问成功
        """
        async def write(session: AsyncSession) -> None:
            """在同一异步事务中读取并更新站点成功统计。"""
            lst_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sta = await SiteStatistic.async_get_by_domain(session, domain)
            if sta:
                note = dict(sta.note) if sta.note else {}
                avg_seconds = None
                if seconds is not None:
                    note[lst_date] = seconds or 1
                    avg_times = len(note.keys())
                    if avg_times > 10:
                        note = dict(sorted(
                            note.items(), key=lambda item: item[0], reverse=True
                        )[:10])
                    avg_seconds = sum(note.values()) // avg_times
                sta.success += 1
                sta.seconds = avg_seconds or sta.seconds
                sta.lst_state = 0
                sta.lst_mod_date = lst_date
                sta.note = note
                return
            note = {lst_date: seconds or 1} if seconds is not None else {}
            session.add(SiteStatistic(
                domain=domain,
                success=1,
                fail=0,
                seconds=seconds or 1,
                lst_state=0,
                lst_mod_date=lst_date,
                note=note,
            ))

        await self._execute_async_write(write)

    async def async_fail(self, domain: str):
        """
        异步站点访问失败
        """
        async def write(session: AsyncSession) -> None:
            """在同一异步事务中读取并更新站点失败统计。"""
            lst_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sta = await SiteStatistic.async_get_by_domain(session, domain)
            if sta:
                sta.fail += 1
                sta.lst_state = 1
                sta.lst_mod_date = lst_date
                return
            session.add(SiteStatistic(
                domain=domain,
                success=0,
                fail=1,
                lst_state=1,
                lst_mod_date=lst_date,
            ))

        await self._execute_async_write(write)
