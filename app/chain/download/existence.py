"""媒体存在性计算与缺失集投影 owner。"""

from typing import Dict, Optional, Tuple, cast

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
    """媒体存在性计算与缺失集投影 owner。"""


    def get_no_exists_info(self, meta: MetaBase,
                           mediainfo: MediaInfo | MusicInfo,
                           no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None,
                           totals: Optional[Dict[int, int]] = None
                           ) -> Tuple[bool, Dict[str, Dict[int, NotExistMediaInfo]]]:
        """
        检查媒体库，查询电影或音乐是否存在；对于剧集同时返回不存在的季集信息
        :param meta: 元数据
        :param mediainfo: 已识别的媒体信息
        :param no_exists: 在调用该方法前已经存储的不存在的季集信息，有传入时该函数搜索的内容将会叠加后输出
        :param totals: 电视剧每季的总集数
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

        mediaserver = self.media_server_repository
        if mediainfo.type == MediaType.MOVIE:
            # 电影
            itemid = mediaserver.get_item_id(mtype=mediainfo.type.value,
                                             title=mediainfo.title,
                                             media_source=media_source,
                                             media_id=media_id)
            exists_movies: Optional[ExistMediaInfo] = self.media_exists(
                mediainfo=cast(MediaInfo, mediainfo),
                itemid=itemid,
            )
            if exists_movies:
                logger.info(f"媒体库中已存在电影：{mediainfo.title_year}")
                return True, {}
            return False, {}
        if mediainfo.type == MediaType.MUSIC:
            # 专辑在媒体库中按一个集合条目判断；单曲则由具体音乐服务器继续按曲名检索。
            itemid = mediaserver.get_item_id(
                mtype=mediainfo.type.value,
                title=mediainfo.title,
                year=mediainfo.year,
            )
            exists_music: Optional[ExistMediaInfo] = self.media_exists(
                mediainfo=cast(MediaInfo, mediainfo),
                itemid=itemid,
            )
            if exists_music:
                logger.info(f"媒体库中已存在音乐：{mediainfo.title_year}")
                return True, {}
            return False, {}
        else:
            if not isinstance(mediainfo, MediaInfo):
                return False, {}
            if not mediainfo.seasons:
                # 补充媒体信息
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
            # 电视剧
            itemid = mediaserver.get_item_id(mtype=mediainfo.type.value,
                                             title=mediainfo.title,
                                             media_source=media_source,
                                             media_id=media_id,
                                             season=mediainfo.season)
            # 媒体库已存在的剧集
            exists_tvs: Optional[ExistMediaInfo] = self.media_exists(mediainfo=mediainfo, itemid=itemid)
            if not exists_tvs:
                # 所有季集均缺失
                for season, episodes in mediainfo.seasons.items():
                    if not episodes:
                        continue
                    # 全季不存在
                    if meta.sea \
                            and season not in meta.season_list:
                        continue
                    # 总集数
                    total_ep = totals.get(season) or len(episodes)
                    self._append_no_exists(
                        no_exists, media_source, media_id, season, [],
                        total_ep, min(episodes)
                    )
                return False, no_exists
            else:
                # 存在一些，检查每季缺失的季集
                for season, episodes in mediainfo.seasons.items():
                    if meta.sea \
                            and season not in meta.season_list:
                        continue
                    if not episodes:
                        continue
                    # 该季总集数
                    season_total = totals.get(season) or len(episodes)
                    # 该季已存在的集
                    exist_episodes = (exists_tvs.seasons or {}).get(season)
                    if exist_episodes:
                        # 已存在取差集
                        if totals.get(season):
                            # 按总集数计算缺失集（开始集为TMDB中的最小集）
                            lack_episodes = list(set(range(min(episodes),
                                                           season_total + min(episodes))
                                                     ).difference(set(exist_episodes)))
                        else:
                            # 按TMDB集数计算缺失集
                            lack_episodes = list(set(episodes).difference(set(exist_episodes)))
                        if not lack_episodes:
                            # 全部集存在
                            continue
                        # 添加不存在的季集信息
                        self._append_no_exists(
                            no_exists, media_source, media_id, season,
                            lack_episodes, season_total, min(lack_episodes)
                        )
                    else:
                        # 全季不存在
                        self._append_no_exists(
                            no_exists, media_source, media_id, season, [],
                            season_total, min(episodes)
                        )
            # 存在不完整的剧集
            if no_exists:
                logger.debug(f"媒体库中已存在部分剧集，缺失：{no_exists}")
                return False, no_exists
            # 全部存在
            return True, no_exists

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
        """把一季缺失信息合并到标准媒体身份对应的结果中。"""
        media_key = build_media_key(media_source, media_id)
        if media_key is None:
            return
        no_exists.setdefault(media_key, {})[season] = NotExistMediaInfo(
            season=season,
            episodes=episodes,
            total_episode=total,
            start_episode=start,
        )
