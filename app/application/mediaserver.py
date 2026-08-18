from typing import Optional

from app.runtime.extensions.service_registry import ServiceBaseHelper
from app.schemas.system import MediaServerConf
from app.schemas.system import ServiceInfo
from app.schemas.types import SystemConfigKey


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
