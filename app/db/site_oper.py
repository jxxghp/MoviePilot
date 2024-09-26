import json
from datetime import datetime
from typing import Tuple, List

from app.db import DbOper
from app.db.models.site import Site
from app.db.models.siteuserdata import SiteUserData
from app.utils.object import ObjectUtils


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

    def update_userdata(self, domain: str, payload: dict) -> Tuple[bool, str]:
        """
        更新站点用户数据
        """
        # 当前系统日期
        current_day = datetime.now().strftime('%Y-%m-%d')
        current_time = datetime.now().strftime('%H:%M:%S')
        payload.update({
            "domain": domain,
            "updated_day": current_day,
            "updated_time": current_time
        })
        siteuserdata = SiteUserData.get_by_domain(self._db, domain=domain,
                                                  workdate=current_day, worktime=current_time)
        if siteuserdata:
            # 存在则更新
            SiteUserData.update(self._db, payload)
        else:
            # 不存在则插入
            for key, value in payload.items():
                if ObjectUtils.is_obj(value):
                    payload[key] = json.dumps(value)
            SiteUserData(**payload).create(self._db)
        return True, "更新站点用户数据成功"

    def get_userdata_by_domain(self, domain: str, workdate: str = None) -> List[SiteUserData]:
        """
        获取站点用户数据
        """
        return SiteUserData.get_by_domain(self._db, domain=domain, workdate=workdate)

    def get_userdata_by_date(self, date: str) -> List[SiteUserData]:
        """
        获取站点用户数据
        """
        return SiteUserData.get_by_date(self._db, date)
