from typing import Tuple, Dict

from cachetools import TTLCache, cached

from app.utils.singleton import Singleton


class ResourceHelper(metaclass=Singleton):
    """
    资源包管理，下载更新资源包
    """

    @cached(cache=TTLCache(maxsize=1, ttl=1800))
    def get_versions(self) -> Dict[str, dict]:
        """
        获取资源包版本信息
        """
        pass

    def check_auth_update(self) -> Tuple[bool, str]:
        """
        检查认证资源是否有新版本
        """
        pass

    def check_sites_update(self) -> Tuple[bool, str]:
        """
        检查站点资源是否有新版本
        """
        pass

    def update_auth(self) -> bool:
        """
        更新认证资源
        """
        pass

    def update_sites(self) -> bool:
        """
        更新站点资源
        """
        pass
