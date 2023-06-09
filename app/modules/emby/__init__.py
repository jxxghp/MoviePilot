from pathlib import Path
from typing import Optional, Tuple, Union, Any

from app import schemas
from app.core.context import MediaInfo
from app.log import logger
from app.modules import _ModuleBase
from app.modules.emby.emby import Emby
from app.schemas import ExistMediaInfo, RefreshMediaItem
from app.schemas.types import MediaType


class EmbyModule(_ModuleBase):
    emby: Emby = None

    def init_module(self) -> None:
        self.emby = Emby()

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "MEDIASERVER", "emby"

    def user_authenticate(self, name: str, password: str) -> Optional[str]:
        """
        使用Emby用户辅助完成用户认证
        :param name: 用户名
        :param password: 密码
        :return: token or None
        """
        # Emby认证
        return self.emby.authenticate(name, password)

    def webhook_parser(self, body: Any, form: Any, args: Any) -> Optional[dict]:
        """
        解析Webhook报文体
        :param body:  请求体
        :param form:  请求表单
        :param args:  请求参数
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        return self.emby.get_webhook_message(form.get("data"))

    def media_exists(self, mediainfo: MediaInfo) -> Optional[ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        if mediainfo.type == MediaType.MOVIE:
            movies = self.emby.get_movies(title=mediainfo.title, year=mediainfo.year)
            if not movies:
                logger.info(f"{mediainfo.title_year} 在媒体库中不存在")
                return None
            else:
                logger.info(f"媒体库中已存在：{movies}")
                return ExistMediaInfo(type=MediaType.MOVIE)
        else:
            tvs = self.emby.get_tv_episodes(title=mediainfo.title,
                                            year=mediainfo.year,
                                            tmdb_id=mediainfo.tmdb_id)
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
        return self.emby.refresh_library_by_items(items)

    def media_statistic(self) -> schemas.Statistic:
        """
        媒体数量统计
        """
        media_statistic = self.emby.get_medias_count()
        user_count = self.emby.get_user_count()
        return schemas.Statistic(
            movie_count=media_statistic.get("MovieCount") or 0,
            tv_count=media_statistic.get("SeriesCount") or 0,
            episode_count=media_statistic.get("EpisodeCount") or 0,
            user_count=user_count or 0
        )
