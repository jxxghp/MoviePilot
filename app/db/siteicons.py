from typing import List

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.db.models.siteicon import SiteIcon


class SiteIcons:
    """
    站点管理
    """
    _db: Session = None

    def __init__(self, _db=SessionLocal()):
        self._db = _db

    def list(self) -> List[SiteIcon]:
        """
        获取站点图标列表
        """
        return SiteIcon.list(self._db)

    def get_by_domain(self, domain: str) -> SiteIcon:
        """
        按域名获取站点图标
        """
        return SiteIcon.get_by_domain(self._db, domain)

    def update_icon(self, name: str, domain: str, icon_url: str, icon_base64: str) -> bool:
        """
        更新站点图标
        """
        siteicon = SiteIcon(name=name, domain=domain, url=icon_url, base64=icon_base64)
        if not self.get_by_domain(domain):
            siteicon.create(self._db)
        elif icon_base64:
            siteicon.update(self._db, {
                "url": icon_url,
                "base64": f"data:image/ico;base64,{icon_base64}"
            })
        return True
