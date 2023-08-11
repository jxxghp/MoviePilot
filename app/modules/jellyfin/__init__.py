import json
from pathlib import Path
from typing import Optional, Tuple, Union, Any, List, Generator

from app import schemas
from app.core.context import MediaInfo
from app.log import logger
from app.modules import _ModuleBase
from app.modules.jellyfin.jellyfin import Jellyfin
from app.schemas import ExistMediaInfo, WebhookEventInfo
from app.schemas.types import MediaType


class JellyfinModule(_ModuleBase):
    jellyfin: Jellyfin = None

    def init_module(self) -> None:
        self.jellyfin = Jellyfin()

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "MEDIASERVER", "jellyfin"

    def user_authenticate(self, name: str, password: str) -> Optional[str]:
        """
        使用Emby用户辅助完成用户认证
        :param name: 用户名
        :param password: 密码
        :return: Token or None
        """
        # Jellyfin认证
        return self.jellyfin.authenticate(name, password)

    def webhook_parser(self, body: Any, form: Any, args: Any) -> WebhookEventInfo:
        """
        解析Webhook报文体
        :param body:  请求体
        :param form:  请求表单
        :param args:  请求参数
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        return self.jellyfin.get_webhook_message(json.loads(body))

    def media_exists(self, mediainfo: MediaInfo, itemid: str = None) -> Optional[ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        if mediainfo.type == MediaType.MOVIE:
            if itemid:
                movie = self.jellyfin.get_iteminfo(itemid)
                if movie:
                    logger.info(f"媒体库中已存在：{movie}")
                    return ExistMediaInfo(type=MediaType.MOVIE)
            movies = self.jellyfin.get_movies(title=mediainfo.title, year=mediainfo.year)
            if not movies:
                logger.info(f"{mediainfo.title_year} 在媒体库中不存在")
                return None
            else:
                logger.info(f"媒体库中已存在：{movies}")
                return ExistMediaInfo(type=MediaType.MOVIE)
        else:
            tvs = self.jellyfin.get_tv_episodes(title=mediainfo.title,
                                                year=mediainfo.year,
                                                tmdb_id=mediainfo.tmdb_id,
                                                item_id=itemid)
            if not tvs:
                logger.info(f"{mediainfo.title_year} 在媒体库中不存在")
                return None
            else:
                logger.info(f"{mediainfo.title_year} 媒体库中已存在：{tvs}")
                return ExistMediaInfo(type=MediaType.TV, seasons=tvs)

    def refresh_mediaserver(self, mediainfo: MediaInfo, file_path: Path) -> Optional[bool]:
        """
        刷新媒体库
        :param mediainfo:  识别的媒体信息
        :param file_path:  文件路径
        :return: 成功或失败
        """
        return self.jellyfin.refresh_root_library()

    def media_statistic(self) -> schemas.Statistic:
        """
        媒体数量统计
        """
        media_statistic = self.jellyfin.get_medias_count()
        user_count = self.jellyfin.get_user_count()
        return schemas.Statistic(
            movie_count=media_statistic.get("MovieCount") or 0,
            tv_count=media_statistic.get("SeriesCount") or 0,
            episode_count=media_statistic.get("EpisodeCount") or 0,
            user_count=user_count or 0
        )

    def mediaserver_librarys(self) -> List[schemas.MediaServerLibrary]:
        """
        媒体库列表
        """
        librarys = self.jellyfin.get_librarys()
        if not librarys:
            return []
        return [schemas.MediaServerLibrary(
            server="jellyfin",
            id=library.get("id"),
            name=library.get("name"),
            type=library.get("type"),
            path=library.get("path")
        ) for library in librarys]

    def mediaserver_items(self, library_id: str) -> Generator:
        """
        媒体库项目列表
        """
        items = self.jellyfin.get_items(library_id)
        for item in items:
            yield schemas.MediaServerItem(
                server="jellyfin",
                library=item.get("library"),
                item_id=item.get("id"),
                item_type=item.get("type"),
                title=item.get("title"),
                original_title=item.get("original_title"),
                year=item.get("year"),
                tmdbid=item.get("tmdbid"),
                imdbid=item.get("imdbid"),
                tvdbid=item.get("tvdbid"),
                path=item.get("path"),
            )

    def mediaserver_tv_episodes(self, item_id: Union[str, int]) -> List[schemas.MediaServerSeasonInfo]:
        """
        获取剧集信息
        """
        seasoninfo = self.jellyfin.get_tv_episodes(item_id=item_id)
        if not seasoninfo:
            return []
        return [schemas.MediaServerSeasonInfo(
            season=season,
            episodes=episodes
        ) for season, episodes in seasoninfo.items()]
