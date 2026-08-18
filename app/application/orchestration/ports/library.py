"""媒体库域的能力端口客户端。"""

from __future__ import annotations

from typing import List, Optional

from app.application.orchestration.ports.dispatch import CapabilityPorts
from app.domain.context import MediaInfo
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.workflow import FileItem


class LibraryPorts(CapabilityPorts):
    """媒体服务器存量判定与文件清单查询的能力端口。"""

    def media_exists(
            self,
            mediainfo: MediaInfo,
            itemid: Optional[str] = None,
            server: Optional[str] = None,
    ) -> Optional[ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID
        :param server:  媒体服务器
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        return self._dispatch.unicast(
            "media_exists", mediainfo=mediainfo, itemid=itemid, server=server
        )

    def media_files(self, mediainfo: MediaInfo) -> Optional[List[FileItem]]:
        """
        获取媒体文件清单
        :param mediainfo:  识别的媒体信息
        :return: 媒体文件列表
        """
        return self._dispatch.unicast("media_files", mediainfo=mediainfo)
