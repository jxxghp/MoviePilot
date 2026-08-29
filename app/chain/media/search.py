"""媒体搜索入口 owner。"""

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
        # 提取要素
        mtype, key_word, season_num, episode_num, year, content = title_rules.parse_search_keyword(title)
        # 识别
        content = content or title
        meta = MetaInfo(content)
        if not meta.name:
            meta.cn_name = content
        # 合并信息
        if mtype:
            meta.type = mtype
        if season_num:
            meta.begin_season = season_num
        if episode_num:
            meta.begin_episode = episode_num
        if year:
            meta.year = year
        # 开始搜索
        logger.info(f"开始搜索媒体信息：{meta.name}")
        medias: Optional[List[MediaInfo]] = self.search_medias(meta=meta, media_source=media_source)
        if not medias:
            logger.warn(f"{meta.name} 没有找到对应的媒体信息！")
            return meta, []
        logger.info(f"{content} 搜索到 {len(medias)} 条相关媒体信息")
        # 识别的元数据，媒体信息列表
        return meta, medias

    async def async_search(
        self, title: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Tuple[Optional[MetaBase], List[MediaInfo]]:
        """
        搜索媒体/人物信息（异步版本）

        :param title: 搜索内容
        :param media_source: 请求级搜索数据源
        :return: 识别元数据，媒体信息列表
        """
        # 提取要素
        mtype, key_word, season_num, episode_num, year, content = title_rules.parse_search_keyword(title)
        # 识别
        content = content or title
        meta = MetaInfo(content)
        if not meta.name:
            meta.cn_name = content
        # 合并信息
        if mtype:
            meta.type = mtype
        if season_num:
            meta.begin_season = season_num
        if episode_num:
            meta.begin_episode = episode_num
        if year:
            meta.year = year
        # 开始搜索
        logger.info(f"开始搜索媒体信息：{meta.name}")
        medias: Optional[List[MediaInfo]] = await self.async_search_medias(meta=meta, media_source=media_source)
        if not medias:
            logger.warn(f"{meta.name} 没有找到对应的媒体信息！")
            return meta, []
        logger.info(f"{content} 搜索到 {len(medias)} 条相关媒体信息")
        # 识别的元数据，媒体信息列表
        return meta, medias
