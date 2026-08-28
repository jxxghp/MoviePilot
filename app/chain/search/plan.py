"""搜索身份、关键词和缺集计划 owner。"""

from copy import deepcopy
from typing import Dict, List, Optional, Tuple, Union

from app.application.configuration import (
    get_chain_runtime_config_snapshot,
)
from app.chain.search.contract import _SearchOwnerBase
from app.domain.context import MediaInfo
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import (
    MediaSource,
)

MissingMediaMap = Dict[str, Dict[int, NotExistMediaInfo]]
SeasonEpisodes = Dict[int, List[int]]
RecognitionArgs = Dict[str, Optional[Union[MediaSource, str]]]


class SearchPlanOwner(_SearchOwnerBase):
    """搜索身份、关键词和缺集计划 owner。"""

    @staticmethod
    def _build_search_keyword(
        media_source: MediaSource,
        media_id: str,
    ) -> str:
        """根据规范媒体身份生成可重放的搜索关键字。"""
        return build_media_key(media_source, media_id)

    @staticmethod
    def _media_recognize_kwargs(mediainfo: MediaInfo) -> RecognitionArgs:
        """从统一媒体信息构造规范识别参数。"""
        media_source, media_id = resolve_media_identity(media=mediainfo)
        return {
            "media_source": media_source,
            "media_id": media_id,
        }

    @staticmethod
    def _copy_media_input(mediainfo: MediaInfo) -> MediaInfo:
        """复制调用方媒体快照，避免搜索归一化污染共享领域对象。"""
        return deepcopy(mediainfo)

    @staticmethod
    def _prepare_params(
        mediainfo: MediaInfo,
        keyword: Optional[str] = None,
        no_exists: Optional[MissingMediaMap] = None,
    ) -> Tuple[Optional[SeasonEpisodes], List[str]]:
        """
        准备搜索参数
        """
        # 缺失的季集
        media_source, media_id = resolve_media_identity(media=mediainfo)
        mediakey = build_media_key(media_source, media_id)
        if no_exists and no_exists.get(mediakey):
            # 过滤剧集
            season_episodes = {season: info.episodes or [] for season, info in no_exists[mediakey].items()}
        elif mediainfo.season is not None:
            # 豆瓣只搜索当前季
            season_episodes = {mediainfo.season: []}
        else:
            season_episodes = None

        # 搜索关键词
        if keyword:
            keywords = [keyword]
        else:
            # 去重去空，但要保持顺序
            keywords = list(
                dict.fromkeys(
                    [
                        k
                        for k in [
                            mediainfo.title,
                            *(mediainfo.names or []),
                            mediainfo.original_title,
                            mediainfo.en_title,
                            mediainfo.hk_title,
                            mediainfo.tw_title,
                            mediainfo.sg_title,
                        ]
                        if k
                    ]
                )
            )
            # 限制搜索关键词数量
            max_names = get_chain_runtime_config_snapshot().max_search_name_limit
            if max_names:
                keywords = keywords[:max_names]

        return season_episodes, keywords
