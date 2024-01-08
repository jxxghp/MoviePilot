import json
from typing import Optional, List

from app import schemas
from app.chain import ChainBase
from app.db.mediaserver_oper import MediaServerOper
from app.utils.singleton import Singleton


class DashboardChain(ChainBase, metaclass=Singleton):

    def __init__(self):
        super().__init__()
        self.dboper = MediaServerOper()

    """
    各类仪表板统计处理链
    """

    def media_statistic(self) -> Optional[schemas.Statistic]:
        """
        媒体数量统计
        """
        ret_statistic = schemas.Statistic()
        media_statistics = self.run_module("media_statistic")
        if media_statistics:
            # 汇总各媒体库统计信息
            for media_statistic in media_statistics:
                ret_statistic.user_count += media_statistic.user_count
            # 电影数量
            movies = self.dboper.list_by_type(mtype="电影") or []
            ret_statistic.movie_count = len(movies)
            # 电视剧数量
            series = self.dboper.list_by_type(mtype="电视剧") or []
            if series:
                ret_statistic.tv_count = len(series)
                # 剧集数量
                for tv in series:
                    seasoninfo = tv.seasoninfo
                    if seasoninfo:
                        if not isinstance(seasoninfo, dict):
                            seasoninfo = json.loads(seasoninfo)
                        if seasoninfo.keys():
                            for season in seasoninfo.keys():
                                episodes = seasoninfo.get(season) or []
                                ret_statistic.episode_count += len(episodes)
        return ret_statistic

    def downloader_info(self) -> schemas.DownloaderInfo:
        """
        下载器信息
        """
        return self.run_module("downloader_info")
