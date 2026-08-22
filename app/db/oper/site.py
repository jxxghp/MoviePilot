from datetime import datetime
from typing import Any, List, Mapping, Tuple, Optional

from sqlalchemy import delete as sqlalchemy_delete

from app.db.base import DbOper
from app.db.models.site import Site
from app.db.models.siteicon import SiteIcon
from app.db.models.sitestatistic import SiteStatistic
from app.db.models.siteuserdata import SiteUserData


class SiteOper(DbOper):
    """
    站点管理
    """

    def add(self, **kwargs) -> Tuple[bool, str]:
        """
        新增站点
        """
        site = Site(**kwargs)
        if not site.get_by_domain(self._db, kwargs.get("domain")):
            site.create(self._db)
            return True, "新增站点成功"
        return False, "站点已存在"

    def get(self, sid: int) -> Optional[Site]:
        """
        查询单个站点
        """
        return Site.get(self._db, sid)

    async def async_get(self, sid: int) -> Optional[Site]:
        """
        异步查询单个站点
        """
        return await Site.async_get(self._db, sid)

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
        """暂存站点字段更新，不由模型装饰器提前提交。"""
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
        return Site.list(self._db)

    async def async_list(self) -> List[Site]:
        """
        异步获取站点列表
        """
        return await Site.async_list(self._db)

    async def async_list_order_by_pri(self) -> List[Site]:
        """异步按优先级获取站点，供站点查询应用服务使用。"""
        return await Site.async_list_order_by_pri(self._db)

    def list_order_by_pri(self) -> List[Site]:
        """
        获取站点列表
        """
        return Site.list_order_by_pri(self._db)

    def list_active(self) -> List[Site]:
        """
        按状态获取站点列表
        """
        return Site.get_actives(self._db)

    async def async_list_active(self) -> List[Site]:
        """
        异步按状态获取站点列表
        """
        return await Site.async_get_actives(self._db)

    def delete(self, sid: int):
        """
        删除站点
        """
        Site.delete(self._db, sid)

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
        site = Site.get(self._db, sid)
        if not site:
            return None
        site.update(self._db, payload)
        return site

    async def async_update(self, sid: int, payload: dict) -> Optional[Site]:
        """
        异步更新站点。
        """
        site = await self.async_get(sid)
        if site:
            await site.async_update(self._db, payload)
        return site

    def get_by_domain(self, domain: str) -> Optional[Site]:
        """
        按域名获取站点
        """
        return Site.get_by_domain(self._db, domain)

    async def async_get_by_domain(self, domain: str) -> Optional[Site]:
        """
        异步按域名获取站点
        """
        return await Site.async_get_by_domain(self._db, domain)

    async def async_get_by_name(self, name: str) -> Optional[Site]:
        """
        异步按名称获取站点
        """
        return await Site.async_get_by_name(self._db, name)

    def get_domains_by_ids(self, ids: List[int]) -> List[Optional[str]]:
        """
        按ID获取站点域名
        """
        return Site.get_domains_by_ids(self._db, ids)

    def exists(self, domain: str) -> bool:
        """
        判断站点是否存在
        """
        return Site.get_by_domain(self._db, domain) is not None

    def update_cookie(self, domain: str, cookies: str) -> Tuple[bool, str]:
        """
        更新站点Cookie
        """
        site = Site.get_by_domain(self._db, domain)
        if not site:
            return False, "站点不存在"
        site.update(self._db, {
            "cookie": cookies
        })
        return True, "更新站点Cookie成功"

    def update_rss(self, domain: str, rss: str) -> Tuple[bool, str]:
        """
        更新站点rss
        """
        site = Site.get_by_domain(self._db, domain)
        if not site:
            return False, "站点不存在"
        site.update(self._db, {
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
        siteuserdatas = SiteUserData.get_by_domain(self._db, domain=domain, workdate=current_day)
        if siteuserdatas:
            # 存在则更新
            if not payload.get("err_msg"):
                siteuserdatas[0].update(self._db, payload)
        else:
            # 不存在则插入
            SiteUserData(**payload).create(self._db)
        return True, "更新站点用户数据成功"

    def get_userdata(self) -> List[SiteUserData]:
        """
        获取站点用户数据
        """
        return SiteUserData.list(self._db)

    def get_userdata_by_domain(self, domain: str, workdate: Optional[str] = None) -> List[SiteUserData]:
        """
        获取站点用户数据
        """
        return SiteUserData.get_by_domain(self._db, domain=domain, workdate=workdate)

    async def async_get_userdata_by_domain(
        self, domain: str, workdate: Optional[str] = None
    ) -> List[SiteUserData]:
        """
        异步获取站点用户数据。
        """
        return await SiteUserData.async_get_by_domain(
            self._db, domain=domain, workdate=workdate
        )

    async def async_get_userdata_latest(self) -> List[SiteUserData]:
        """异步获取各站点最新用户数据。"""
        return await SiteUserData.async_get_latest(self._db)

    async def async_get_icon_by_domain(self, domain: str) -> Optional[SiteIcon]:
        """异步按域名获取站点图标。"""
        return await SiteIcon.async_get_by_domain(self._db, domain)

    async def async_get_statistic_by_domain(
        self,
        domain: str,
    ) -> Optional[SiteStatistic]:
        """异步按域名获取站点统计。"""
        return await SiteStatistic.async_get_by_domain(self._db, domain)

    async def async_list_statistics(self) -> List[SiteStatistic]:
        """异步获取所有站点统计。"""
        return await SiteStatistic.async_list(self._db)

    def get_userdata_by_date(self, date: str) -> List[SiteUserData]:
        """
        获取站点用户数据
        """
        return SiteUserData.get_by_date(self._db, date)

    def get_userdata_latest(self) -> List[SiteUserData]:
        """
        获取站点最新数据
        """
        return SiteUserData.get_latest(self._db)

    def get_icon_by_domain(self, domain: str) -> Optional[SiteIcon]:
        """
        按域名获取站点图标
        """
        return SiteIcon.get_by_domain(self._db, domain)

    def update_icon(self, name: str, domain: str, icon_url: str, icon_base64: str) -> bool:
        """
        更新站点图标
        """
        icon_base64 = f"data:image/ico;base64,{icon_base64}" if icon_base64 else ""
        siteicon = self.get_icon_by_domain(domain)
        if not siteicon:
            SiteIcon(name=name, domain=domain, url=icon_url, base64=icon_base64).create(self._db)
        elif icon_base64:
            siteicon.update(self._db, {
                "url": icon_url,
                "base64": icon_base64
            })
        return True

    def success(self, domain: str, seconds: Optional[int] = None):
        """
        站点访问成功
        """
        lst_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sta = SiteStatistic.get_by_domain(self._db, domain)
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

            sta.update(self._db, {
                "success": sta.success + 1,
                "seconds": avg_seconds or sta.seconds,
                "lst_state": 0,
                "lst_mod_date": lst_date,
                "note": note
            })
        else:
            note = {}
            if seconds is not None:
                note = {
                    lst_date: seconds or 1
                }
            SiteStatistic(
                domain=domain,
                success=1,
                fail=0,
                seconds=seconds or 1,
                lst_state=0,
                lst_mod_date=lst_date,
                note=note
            ).create(self._db)

    def fail(self, domain: str):
        """
        站点访问失败
        """
        lst_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sta = SiteStatistic.get_by_domain(self._db, domain)
        if sta:
            sta.update(self._db, {
                "fail": sta.fail + 1,
                "lst_state": 1,
                "lst_mod_date": lst_date
            })
        else:
            SiteStatistic(
                domain=domain,
                success=0,
                fail=1,
                lst_state=1,
                lst_mod_date=lst_date
            ).create(self._db)

    async def async_success(self, domain: str, seconds: Optional[int] = None):
        """
        异步站点访问成功
        """
        lst_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sta = await SiteStatistic.async_get_by_domain(self._db, domain)
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

            await sta.async_update(self._db, {
                "success": sta.success + 1,
                "seconds": avg_seconds or sta.seconds,
                "lst_state": 0,
                "lst_mod_date": lst_date,
                "note": note
            })
        else:
            note = {}
            if seconds is not None:
                note = {
                    lst_date: seconds or 1
                }
            await SiteStatistic(
                domain=domain,
                success=1,
                fail=0,
                seconds=seconds or 1,
                lst_state=0,
                lst_mod_date=lst_date,
                note=note
            ).async_create(self._db)

    async def async_fail(self, domain: str):
        """
        异步站点访问失败
        """
        lst_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sta = await SiteStatistic.async_get_by_domain(self._db, domain)
        if sta:
            await sta.async_update(self._db, {
                "fail": sta.fail + 1,
                "lst_state": 1,
                "lst_mod_date": lst_date
            })
        else:
            await SiteStatistic(
                domain=domain,
                success=0,
                fail=1,
                lst_state=1,
                lst_mod_date=lst_date
            ).async_create(self._db)
