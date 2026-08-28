"""保留旧站点 Oper 导入路径和无 Session 插件调用形态。"""

from app.db.oper.site import SiteOper as CanonicalSiteOper


class SiteOper(CanonicalSiteOper):
    """继承表级站点 Oper，并将旧插件入口隔离在 Legacy SDK。"""


__all__ = ["SiteOper"]
