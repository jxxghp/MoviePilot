from typing import List

from app.db import DbOper
from app.db.models.siteicon import SiteIcon


class SiteIconOper(DbOper):
    """
    站点管理
    """

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
        icon_base64 = f"data:image/ico;base64,{icon_base64}" if icon_base64 else ""
        siteicon = self.get_by_domain(domain)
        if not siteicon:
            SiteIcon(name=name, domain=domain, url=icon_url, base64=icon_base64).create(self._db)
        elif icon_base64:
            siteicon.update(self._db, {
                "url": icon_url,
                "base64": icon_base64
            })
        return True
