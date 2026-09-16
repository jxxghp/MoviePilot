"""搜索身份、关键词和缺集计划 owner。"""

from copy import deepcopy
from typing import Dict, List, Optional, Tuple, Union, cast

from app.application.configuration import (
    get_chain_runtime_config_snapshot,
)
from app.chain.search.contract import _SearchOwnerBase
from app.chain.search.music import SearchMusicOwner
from app.domain.context import MediaInfo, MusicInfo
from app.domain.metainfo import MetaInfo
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import (
    MediaSource,
)

MissingMediaMap = Dict[str, Dict[int, NotExistMediaInfo]]
SeasonEpisodes = Dict[int, List[int]]
RecognitionArgs = Dict[str, Optional[Union[MediaSource, str]]]


def _limit_search_names(keywords: List[str], explicit_keyword: Optional[str]) -> List[str]:
    """统一遵守用户设置的名称查询上限，显式关键词不受名称展开策略影响。"""
    max_names = get_chain_runtime_config_snapshot().max_search_name_limit
    return keywords[:max_names] if max_names and not explicit_keyword else keywords


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
    def _copy_media_input(mediainfo: MediaInfo | MusicInfo) -> MediaInfo | MusicInfo:
        """复制调用方媒体快照，避免搜索归一化污染共享领域对象。"""
        return deepcopy(mediainfo)

    @staticmethod
    def _prepare_media_input(mediainfo: MediaInfo | MusicInfo) -> MediaInfo | MusicInfo:
        """只规范化影视输入的标题和季号，音乐保留其实体字段及原始名称。"""
        if not isinstance(mediainfo, MusicInfo) and not mediainfo.tmdb_id:
            meta = MetaInfo(title=mediainfo.title)
            mediainfo.title = meta.name
            mediainfo.season = cast(int, meta.begin_season)
        return mediainfo

    @staticmethod
    def _needs_media_details(mediainfo: MediaInfo | MusicInfo) -> bool:
        """音乐已有主名称即可匹配，影视别名为空时仍保留既有详情补全策略。"""
        return not mediainfo.names and not (isinstance(mediainfo, MusicInfo) and mediainfo.title)

    @staticmethod
    def _prepare_params(
        mediainfo: MediaInfo | MusicInfo,
        keyword: Optional[str] = None,
        no_exists: Optional[MissingMediaMap] = None,
        include_candidates: bool = False,
    ) -> Tuple[Optional[SeasonEpisodes], List[str]]:
        """
        准备搜索参数
        """
        if isinstance(mediainfo, MusicInfo):
            return None, _limit_search_names(SearchMusicOwner._music_keywords(mediainfo, keyword, include_candidates), keyword)
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
        return season_episodes, _limit_search_names(keywords, keyword)
