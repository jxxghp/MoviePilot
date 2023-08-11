from pathlib import Path
from typing import Optional, Tuple, Union, Any, List, Generator

from app import schemas
from app.core.context import MediaInfo
from app.log import logger
from app.modules import _ModuleBase
from app.modules.plex.plex import Plex
from app.schemas import ExistMediaInfo, RefreshMediaItem, WebhookEventInfo
from app.schemas.types import MediaType


class PlexModule(_ModuleBase):

    plex: Plex = None

    def init_module(self) -> None:
        self.plex = Plex()

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "MEDIASERVER", "plex"

    def webhook_parser(self, body: Any, form: Any, args: Any) -> WebhookEventInfo:
        """
        解析Webhook报文体
        :param body:  请求体
        :param form:  请求表单
        :param args:  请求参数
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        return self.plex.get_webhook_message(form.get("payload"))

    def media_exists(self, mediainfo: MediaInfo, itemid: Optional[str] = None) -> Optional[ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        if mediainfo.type == MediaType.MOVIE:
            movies = self.plex.get_movies(title=mediainfo.title, year=mediainfo.year)
            if not movies:
                logger.info(f"{mediainfo.title_year} 在媒体库中不存在")
                return None
            else:
                logger.info(f"媒体库中已存在：{movies}")
                return ExistMediaInfo(type=MediaType.MOVIE)
        else:
            tvs = self.plex.get_tv_episodes(title=mediainfo.title,
                                            year=mediainfo.year)
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
        items = [
            RefreshMediaItem(
                title=mediainfo.title,
                year=mediainfo.year,
                type=mediainfo.type,
                category=mediainfo.category,
                target_path=file_path
            )
        ]
        return self.plex.refresh_library_by_items(items)

    def media_statistic(self) -> schemas.Statistic:
        """
        媒体数量统计
        """
        media_statistic = self.plex.get_medias_count()
        return schemas.Statistic(
            movie_count=media_statistic.get("MovieCount") or 0,
            tv_count=media_statistic.get("SeriesCount") or 0,
            episode_count=media_statistic.get("EpisodeCount") or 0,
            user_count=1
        )

    def mediaserver_librarys(self) -> List[schemas.MediaServerLibrary]:
        """
        媒体库列表
        """
        librarys = self.plex.get_librarys()
        if not librarys:
            return []
        return [schemas.MediaServerLibrary(
            server="plex",
            id=library.get("id"),
            name=library.get("name"),
            type=library.get("type"),
            path=library.get("path")
        ) for library in librarys]

    def mediaserver_items(self, library_id: str) -> Generator:
        """
        媒体库项目列表
        """
        items = self.plex.get_items(library_id)
        for item in items:
            yield schemas.MediaServerItem(
                server="plex",
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
        seasoninfo = self.plex.get_tv_episodes(item_id=item_id)
        if not seasoninfo:
            return []
        return [schemas.MediaServerSeasonInfo(
            season=season,
            episodes=episodes
        ) for season, episodes in seasoninfo.items()]
