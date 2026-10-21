"""
删除站点后的搜索范围配置清理。
"""

from app.application.configuration import (
    get_configured_system_config,
)
from app.chain.search.contract import _SearchOwnerBase
from app.runtime.events import Event
from app.schemas.types import (
    SystemConfigKey,
)


class SearchSiteOwner(_SearchOwnerBase):
    """
    删除站点后的搜索范围配置清理。
    """

    def _remove_site(self, event: Event) -> None:
        """
        从搜索站点中移除与已删除站点相关的设置
        """
        if not event:
            return
        event_data = event.event_data if isinstance(event.event_data, dict) else {}
        site_id = event_data.get("site_id")
        if not site_id:
            return
        if site_id == "*":
            get_configured_system_config().set(SystemConfigKey.IndexerSites, [])
            return
        selected_sites = get_configured_system_config().get(SystemConfigKey.IndexerSites) or []
        if site_id in selected_sites:
            selected_sites.remove(site_id)
            get_configured_system_config().set(SystemConfigKey.IndexerSites, selected_sites)
