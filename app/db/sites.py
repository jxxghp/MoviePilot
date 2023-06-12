from typing import Tuple, List

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.db.models.site import Site, SiteIcon


class Sites:
    """
    站点管理
    """
    _db: Session = None

    def __init__(self, _db=SessionLocal()):
        self._db = _db

    def add(self, **kwargs) -> Tuple[bool, str]:
        """
        新增站点
        """
        site = Site(**kwargs)
        if not site.get_by_domain(self._db, kwargs.get("domain")):
            site.create(self._db)
            return True, "新增站点成功"
        return False, "站点已存在"

    def list(self) -> List[Site]:
        """
        获取站点列表
        """
        return Site.list(self._db)

    def list_active(self):
        """
        按状态获取站点列表
        """
        return Site.get_actives(self._db)

    def get_by_domain(self, domain: str) -> Site:
        """
        按域名获取站点
        """
        return Site.get_by_domain(self._db, domain)

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

    def update_icon(self, name: str, domain: str, icon_url: str, icon_base64: str) -> bool:
        """
        更新站点图标
        """
        siteicon = SiteIcon(name=name, domain=domain, url=icon_url, base64=icon_base64)
        if not siteicon.get_by_domain(self._db, domain):
            siteicon.create(self._db)
        elif icon_base64:
            siteicon.update(self._db, {
                "url": icon_url,
                "base64": f"data:image/ico;base64,{icon_base64}"
            })
        return True
