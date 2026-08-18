"""媒体库域的能力端口客户端。"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from app.application.orchestration.ports.dispatch import CapabilityPorts
from app.domain.context import MediaInfo
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.types import MediaType
from app.schemas.workflow import FileItem


class LibraryPorts(CapabilityPorts):
    """媒体服务器与文件系统存量判定、文件清单查询的能力端口。"""

    def media_exists(
            self,
            mediainfo: MediaInfo,
            itemid: Optional[str] = None,
            server: Optional[str] = None,
    ) -> Optional[ExistMediaInfo]:
        """
        判断媒体文件是否存在于媒体服务器或文件系统

        电视剧收齐各来源答案后按季集取并集；电影与音乐取首个认领的答案。
        指定 server 时只向该媒体服务器提问，文件系统来源不参与。
        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID，仅媒体服务器来源使用
        :param server:  媒体服务器名称，指定后收窄为单一媒体服务器应答
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        if server or getattr(mediainfo, "type", None) != MediaType.TV:
            return self._dispatch.unicast(
                "media_exists", mediainfo=mediainfo, itemid=itemid, server=server
            )
        return self._merge_exist_seasons(
            self._dispatch.multicast(
                "media_exists", mediainfo=mediainfo, itemid=itemid, server=server
            )
        )

    @staticmethod
    def _merge_exist_seasons(
            results: Sequence[ExistMediaInfo],
    ) -> Optional[ExistMediaInfo]:
        """
        合并各来源认领的电视剧季集

        季集按季号取并集，媒体库标识沿用最高优先级来源的答案。
        :param results:  各来源按优先级排列的存量判定结果
        :return: 合并后的存量信息；无人认领时返回None
        """
        if not results:
            return None
        seasons: Dict[int, List[int]] = {}
        for result in results:
            for season, episodes in (result.seasons or {}).items():
                seasons[season] = sorted(
                    set(seasons.get(season) or []) | set(episodes or [])
                )
        return results[0].model_copy(update={"seasons": seasons})

    def media_files(self, mediainfo: MediaInfo) -> Optional[List[FileItem]]:
        """
        获取媒体文件清单
        :param mediainfo:  识别的媒体信息
        :return: 媒体文件列表
        """
        return self._dispatch.unicast("media_files", mediainfo=mediainfo)
