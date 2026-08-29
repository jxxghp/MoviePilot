"""媒体搜索入口 owner。"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.chain.media.contract import _MediaOwnerBase
from app.domain import title as title_rules
from app.domain.context import (
    MediaInfo,
)
from app.domain.meta.metabase import MetaBase
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.schemas.types import (
    MediaSourceSelection,
)


@dataclass(frozen=True, slots=True)
class _MediaSearchRequest:
    """保存同步和异步搜索共同使用的输入投影。"""

    content: str
    meta: MetaBase


def _build_media_search_request(title: str) -> _MediaSearchRequest:
    """将搜索文本一次性投影为规范元数据，避免双入口规则漂移。"""
    mtype, _, season_num, episode_num, year, content = title_rules.parse_search_keyword(title)
    content = content or title
    meta = MetaInfo(content)
    if not meta.name:
        meta.cn_name = content
    if mtype:
        meta.type = mtype
    if season_num:
        meta.begin_season = season_num
    if episode_num:
        meta.begin_episode = episode_num
    if year:
        meta.year = year
    return _MediaSearchRequest(content=content, meta=meta)


def _finish_media_search(
    request: _MediaSearchRequest,
    medias: Optional[List[MediaInfo]],
) -> Tuple[MetaBase, List[MediaInfo]]:
    """统一空结果与成功结果投影，并保持既有日志语义。"""
    if not medias:
        logger.warn(f"{request.meta.name} 没有找到对应的媒体信息！")
        return request.meta, []
    logger.info(f"{request.content} 搜索到 {len(medias)} 条相关媒体信息")
    return request.meta, medias


class MediaSearchOwner(_MediaOwnerBase):
    """媒体搜索入口 owner。"""

    def search(
        self, title: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Tuple[Optional[MetaBase], List[MediaInfo]]:
        """
        搜索媒体/人物信息

        :param title: 搜索内容
        :param media_source: 请求级搜索数据源
        :return: 识别元数据，媒体信息列表
        """
        request = _build_media_search_request(title)
        logger.info(f"开始搜索媒体信息：{request.meta.name}")
        medias: Optional[List[MediaInfo]] = self.search_medias(
            meta=request.meta,
            media_source=media_source,
        )
        return _finish_media_search(request, medias)

    async def async_search(
        self, title: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Tuple[Optional[MetaBase], List[MediaInfo]]:
        """
        搜索媒体/人物信息（异步版本）

        :param title: 搜索内容
        :param media_source: 请求级搜索数据源
        :return: 识别元数据，媒体信息列表
        """
        request = _build_media_search_request(title)
        logger.info(f"开始搜索媒体信息：{request.meta.name}")
        medias: Optional[List[MediaInfo]] = await self.async_search_medias(
            meta=request.meta,
            media_source=media_source,
        )
        return _finish_media_search(request, medias)
