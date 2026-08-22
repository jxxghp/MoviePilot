"""索引与字幕扩展查询站点资源的端口槽位。

站点索引配置与访问频率限制状态由站点资源实现维护，索引与字幕扩展只声明
用得到的最小协议，具体实现（或测试桩）由组合根注入。
"""

from typing import List, Optional, Protocol, Tuple

from app.runtime.hostports.port import HostPort


class SiteResourceProvider(Protocol):
    """索引与字幕扩展所需的站点资源查询能力。"""

    def get_indexers(self) -> List[dict]:
        """返回全部可用站点索引配置。"""
        ...

    def get_indexer(self, domain: str) -> Optional[dict]:
        """按域名返回单个站点索引配置。"""
        ...

    def check(self, domain: str) -> Tuple[bool, str]:
        """检查站点域名是否触发访问频率限制。"""
        ...


site_resource_port: HostPort[SiteResourceProvider] = HostPort("site_resource")
