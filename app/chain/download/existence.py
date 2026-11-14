"""
媒体存在性计算与缺失集投影 owner。
"""

from typing import Any, Dict, Optional, Tuple, cast

from app.application.mediaserver import MEDIA_SERVER_EXISTENCE_QUERY, get_mediaserver_configs
from app.chain.download.contract import _DownloadOwnerBase
from app.chain.media import MediaChain
from app.domain.context import (
    MediaInfo,
    MusicInfo,
)
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.mediaserver import ExistMediaInfo, NotExistMediaInfo
from app.schemas.types import (
    MediaSource,
    MediaType,
)


class DownloadExistenceOwner(_DownloadOwnerBase):
    """
    媒体存在性计算与缺失集投影 owner。
    """

    def get_no_exists_info(self, meta: MetaBase,
                           mediainfo: MediaInfo | MusicInfo,
                           no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None,
                           totals: Optional[Dict[int, int]] = None,
                           *,
                           totals_are_episode_numbers: bool = False,
                           ) -> Tuple[bool, Dict[str, Dict[int, NotExistMediaInfo]]]:
        """
        检查媒体库，查询电影或音乐是否存在；对于剧集同时返回不存在的季集信息

        :param meta: 元数据
        :param mediainfo: 已识别的媒体信息，指定剧集组时重新识别对应的季集结构
        :param no_exists: 在调用该方法前已经存储的不存在的季集信息，有传入时该函数搜索的内容将会叠加后输出
        :param totals: 电视剧每季的总集数；非正数视为未指定
        :param totals_are_episode_numbers: 指定的总集数为季内末集号；早于季首时沿用原有集数口径
        :return: 当前媒体是否缺失，各标题总的季集和缺失的季集
        """
        media_source, media_id = resolve_media_identity(media=mediainfo)
        if mediainfo.type == MediaType.TV and not build_media_key(media_source, media_id):
            logger.error("电视剧缺集检查需要有效的 media_source 和 media_id")
            return False, no_exists or {}

        if not no_exists:
            no_exists = {}

        if not totals:
            totals = {}

        if mediainfo.type == MediaType.MOVIE:
            exists_movies = self._media_exists_with_server_cache(
                mediainfo=cast(MediaInfo, mediainfo),
                query={
                    "mtype": (mediainfo.type.value if mediainfo.type else ""),
                    "title": mediainfo.title,
                    "year": mediainfo.year,
                    "media_source": media_source,
                    "media_id": media_id,
                },
            )
            if exists_movies:
                logger.info(f"媒体库中已存在电影：{mediainfo.title_year}")
                return True, {}
            return False, {}
        if mediainfo.type == MediaType.MUSIC:
            # 专辑在媒体库中按一个集合条目判断；单曲则由具体音乐服务器继续按曲名检索。
            exists_music = self._media_exists_with_server_cache(
                mediainfo=cast(MediaInfo, mediainfo),
                query={
                    "mtype": (mediainfo.type.value if mediainfo.type else ""),
                    "title": mediainfo.title,
                    "year": mediainfo.year,
                },
            )
            if exists_music:
                logger.info(f"媒体库中已存在音乐：{mediainfo.title_year}")
                return True, {}
            return False, {}
        else:
            if not isinstance(mediainfo, MediaInfo):
                return False, {}
            # 请求中的季信息可能来自默认排序，选择剧集组后必须按该组重新识别。
            if not mediainfo.seasons or mediainfo.episode_group:
                recognized_media = MediaChain().recognize_media(
                    mtype=mediainfo.type,
                    media_source=media_source,
                    media_id=media_id,
                    episode_group=mediainfo.episode_group,
                )
                if not recognized_media:
                    logger.error("媒体信息识别失败！")
                    return False, {}
                mediainfo = recognized_media
                if not mediainfo.seasons:
                    logger.error(f"媒体信息中没有季集信息：{mediainfo.title_year}")
                    return False, {}
            exists_tvs = self._media_exists_with_server_cache(
                mediainfo=mediainfo,
                query={
                    "mtype": (mediainfo.type.value if mediainfo.type else ""),
                    "title": mediainfo.title,
                    "year": mediainfo.year,
                    "media_source": media_source,
                    "media_id": media_id,
                    "season": mediainfo.season,
                },
            )
            existing_seasons = (exists_tvs.seasons or {}) if exists_tvs else {}
            for season, episodes in mediainfo.seasons.items():
                if not episodes or (meta.sea and season not in meta.season_list):
                    continue
                first = min(episodes)
                total_override = max(totals.get(season) or 0, 0)
                season_total = total_override or len(episodes)
                episode_end = total_override if totals_are_episode_numbers and total_override >= first else 0
                exist_episodes = existing_seasons.get(season)
                if exist_episodes:
                    episode_range = range(first, (episode_end or first + season_total - 1) + 1)
                    target_episodes = episode_range if total_override else episodes
                    lack_episodes = sorted(set(target_episodes).difference(exist_episodes))
                    if not lack_episodes:
                        continue
                    first = lack_episodes[0]
                else:
                    lack_episodes = self._whole_season_missing(episodes, season_total, episode_end)
                self._append_no_exists(
                    no_exists, media_source, media_id, season, lack_episodes, season_total, first,
                )
            if exists_tvs and no_exists:
                logger.debug(f"媒体库中已存在部分剧集，缺失：{no_exists}")
            return bool(exists_tvs) and not no_exists, no_exists

    def _media_exists_with_server_cache(
        self,
        mediainfo: MediaInfo,
        query: dict[str, Any],
    ) -> Optional[ExistMediaInfo]:
        """
        按媒体服务器隔离缓存条目，并合并各服务器已确认存在的剧集。
        """
        server_names = [str(config.name) for config in get_mediaserver_configs() if config.name]
        if not server_names:
            itemid = self.media_server_repository.get_item_id(**query)
            return self.media_exists(mediainfo=mediainfo, itemid=itemid, query=MEDIA_SERVER_EXISTENCE_QUERY)
        combined: Optional[ExistMediaInfo] = None
        seasons: dict[int, set[int]] = {}
        for server in server_names:
            itemid = self.media_server_repository.get_item_id(
                server=server,
                **query,
            )
            exists = self.media_exists(
                mediainfo=mediainfo, itemid=itemid, server=server, query=MEDIA_SERVER_EXISTENCE_QUERY
            )
            if exists:
                if mediainfo.type != MediaType.TV:
                    return exists
                combined = combined or exists
                for season, episodes in (exists.seasons or {}).items():
                    seasons.setdefault(season, set()).update(episodes)
        return combined.model_copy(update={
            "seasons": {season: sorted(episodes) for season, episodes in seasons.items()},
        }) if combined else None

    @staticmethod
    def _whole_season_missing(episodes: list[int], total: int, episode_end: int) -> list[int]:
        """
        计算一季完全未入库时的缺集列表。

        手工订阅末集号裁短本季，或恰好覆盖累计编号的整季时，写出目标范围。
        范围内尚未确认的集号继续等待补集；超过本季已知末集号时沿用整季标记。
        仅提供集数的调用继续在季初连续且两种口径一致时写出显式目标集。

        :param episodes: 本季全部集号
        :param total: 本季有效总集数，调用方已保证为正
        :param episode_end: 显式末集号；未指定或早于季首时为 0
        :return: 显式目标集；空列表表示按整季处理
        """
        if episode_end:
            first = min(episodes)
            last = max(episodes)
            if episode_end < last or (first != 1 and episode_end == last):
                return list(range(first, episode_end + 1))
            return []
        if total >= len(episodes):
            return []
        target = sorted(episodes)[:total]
        if target[-1] != total:
            return []
        return target

    @staticmethod
    def _append_no_exists(
            no_exists: Dict[str, Dict[int, NotExistMediaInfo]],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
            season: int,
            episodes: list[int],
            total: int,
            start: int,
    ) -> None:
        """
        把一季缺失信息合并到标准媒体身份对应的结果中。
        """
        media_key = build_media_key(media_source, media_id)
        if media_key is None:
            return
        no_exists.setdefault(media_key, {})[season] = NotExistMediaInfo(
            season=season,
            episodes=episodes,
            total_episode=total,
            start_episode=start,
        )
