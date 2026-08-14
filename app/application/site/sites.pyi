from typing import List, Optional, Tuple


class SitesHelper:
    """声明 Cython 站点认证与索引扩展的宿主接口。"""

    def __init__(self) -> None:
        """返回进程内共享的站点助手实例。"""
        ...

    @property
    def auth_version(self) -> str:
        """返回认证资源版本。"""
        ...

    @property
    def indexer_version(self) -> str:
        """返回站点索引资源版本。"""
        ...

    @property
    def auth_level(self) -> int:
        """返回当前用户认证等级。"""
        ...

    def check(self, domain: str) -> Tuple[bool, str]:
        """检查站点域名是否触发访问频率限制。"""
        ...

    def get_indexers(self) -> List[dict]:
        """返回全部可用站点索引配置。"""
        ...

    async def async_get_indexers(self) -> List[dict]:
        """异步返回全部可用站点索引配置。"""
        ...

    def get_indexer(self, domain: str) -> Optional[dict]:
        """按域名返回单个站点索引配置。"""
        ...

    async def async_get_indexer(self, domain: str) -> Optional[dict]:
        """异步按域名返回单个站点索引配置。"""
        ...

    def get_authsites(self) -> dict:
        """返回认证站点配置。"""
        ...

    def get_indexsites(self) -> dict:
        """返回内置站点索引配置。"""
        ...

    def check_user(
        self,
        site: Optional[str] = None,
        params: Optional[dict] = None,
    ) -> Tuple[bool, str]:
        """校验用户站点认证信息并返回状态与消息。"""
        ...
