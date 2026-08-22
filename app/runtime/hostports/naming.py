"""媒体命名上下文的宿主服务端口。

扩展层按本协议获取重命名模板可用的命名变量，实现由组合根注入。
"""

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from app.runtime.hostports.port import HostPort
from app.schemas.tmdb import TmdbEpisode


@runtime_checkable
class NamingContextProvider(Protocol):
    """媒体命名变量上下文的构建协议。"""

    def build_naming_context(
            self,
            meta: Optional[Any] = None,
            mediainfo: Optional[Any] = None,
            file_extension: Optional[str] = None,
            episodes_info: Optional[List[TmdbEpisode]] = None,
    ) -> Dict[str, Any]:
        """
        构建重命名可用的命名变量上下文。

        :param meta: 文件元数据（``app.domain.meta.metabase.MetaBase``）
        :param mediainfo: 识别的媒体信息（``app.domain.context.MediaInfo``）
        :param file_extension: 文件扩展名
        :param episodes_info: 当前季的全部集信息
        :return: 命名变量上下文字典
        """
        ...


naming_context_port: HostPort[NamingContextProvider] = HostPort("命名上下文")
