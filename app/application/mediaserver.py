from typing import Any, Optional, Protocol

from app.runtime.extensions.service_registry import ServiceBaseHelper
from app.schemas.system import MediaServerConf
from app.schemas.system import ServiceInfo
from app.schemas.types import MediaSource, SystemConfigKey


class AsyncMediaServerQueryRepository(Protocol):
    """媒体服务器本地条目查询所需的异步持久化端口。"""

    async def async_exists(self, **kwargs: Any) -> Any | None:
        """按标题或统一媒体身份查找已同步条目。"""
        ...


class MediaServerQueryService:
    """封装媒体服务器本地存在性查询与 ORM 投影。"""

    def __init__(self, repository: AsyncMediaServerQueryRepository):
        """使用显式媒体服务器查询端口初始化服务。"""
        self._repository = repository

    async def find_item_id(
            self,
            *,
            title: Optional[str] = None,
            year: Optional[str] = None,
            mtype: Optional[str] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            season: Optional[int] = None,
    ) -> Optional[str]:
        """返回匹配条目的服务器 item_id，未命中时返回 None。"""
        item = await self._repository.async_exists(
            title=title,
            year=year,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            season=season,
        )
        return item.item_id if item else None


class MediaServerHelper(ServiceBaseHelper[MediaServerConf]):
    """管理媒体服务器配置，并按类型发现已启用的服务实例。"""

    def __init__(self) -> None:
        """绑定媒体服务器配置键与配置模型。"""
        super().__init__(
            config_key=SystemConfigKey.MediaServers,
            conf_type=MediaServerConf,
        )

    def is_media_server(
        self,
        service_type: Optional[str] = None,
        service: Optional[ServiceInfo] = None,
        name: Optional[str] = None,
    ) -> bool:
        """判断给定服务或服务名称是否属于指定媒体服务器类型。"""
        service = service or self.get_service(name=name)
        return bool(service and service.type == service_type)
