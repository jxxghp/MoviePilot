from datetime import datetime
from typing import List, Tuple, Optional

from app.db import DbOper
from app.db.models import SiteIcon
from app.db.models.site import Site
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

    def get(self, sid: int) -> Site:
        """
        查询单个站点
        """
        return Site.get(self._db, sid)

    def list(self) -> List[Site]:
        """
        获取站点列表
        """
        return Site.list(self._db)

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

    def delete(self, sid: int):
        """
        删除站点
        """
        Site.delete(self._db, sid)

    def update(self, sid: int, payload: dict) -> Site:
        """
        更新站点
        """
        site = Site.get(self._db, sid)
        site.update(self._db, payload)
        return site

    def get_by_domain(self, domain: str) -> Site:
        """
        按域名获取站点
        """
        return Site.get_by_domain(self._db, domain)

    def get_domains_by_ids(self, ids: List[int]) -> List[str]:
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

    def get_userdata_by_domain(self, domain: str, workdate: Optional[str] =  None) -> List[SiteUserData]:
        """
        获取站点用户数据
        """
        return SiteUserData.get_by_domain(self._db, domain=domain, workdate=workdate)

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

    def get_icon_by_domain(self, domain: str) -> SiteIcon:
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

    def success(self, domain: str, seconds: Optional[int] =  None):
        """
        站点访问成功
        """
        lst_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sta = SiteStatistic.get_by_domain(self._db, domain)
        if sta:
            avg_seconds, note = None, {}
            if seconds is not None:
                note: dict = sta.note or {}
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
                "note": note or sta.note
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
