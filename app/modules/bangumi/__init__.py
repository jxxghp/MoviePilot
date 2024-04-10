from typing import List, Optional, Tuple, Union

from app.core.context import MediaInfo
from app.log import logger
from app.modules import _ModuleBase
from app.modules.bangumi.bangumi import BangumiApi
from app.utils.http import RequestUtils


class BangumiModule(_ModuleBase):
    bangumiapi: BangumiApi = None

    def init_module(self) -> None:
        self.bangumiapi = BangumiApi()

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        with RequestUtils().get_res("https://api.bgm.tv/") as ret:
            if ret and ret.status_code == 200:
                return True, ""
            elif ret:
                return False, f"无法连接Bangumi，错误码：{ret.status_code}"
        return False, "Bangumi网络连接失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def recognize_media(self, bangumiid: int = None,
                        **kwargs) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param bangumiid: 识别的Bangumi ID
        :return: 识别的媒体信息，包括剧集信息
        """
        if not bangumiid:
            return None

        # 直接查询详情
        info = self.bangumi_info(bangumiid=bangumiid)
        if info:
            # 赋值TMDB信息并返回
            mediainfo = MediaInfo(bangumi_info=info)
            logger.info(f"{bangumiid} Bangumi识别结果：{mediainfo.type.value} "
                        f"{mediainfo.title_year}")
            return mediainfo
        else:
            logger.info(f"{bangumiid} 未匹配到Bangumi媒体信息")

        return None

    def bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息
        :param bangumiid: BangumiID
        :return: Bangumi信息
        """
        if not bangumiid:
            return None
        logger.info(f"开始获取Bangumi信息：{bangumiid} ...")
        return self.bangumiapi.detail(bangumiid)

    def bangumi_calendar(self, page: int = 1, count: int = 30) -> Optional[List[dict]]:
        """
        获取Bangumi每日放送
        :param page:  页码
        :param count:  每页数量
        """
        return self.bangumiapi.calendar(page, count)

    def bangumi_credits(self, bangumiid: int, page: int = 1, count: int = 20) -> List[dict]:
        """
        根据TMDBID查询电影演职员表
        :param bangumiid:  BangumiID
        :param page:  页码
        :param count:  数量
        """
        persons = self.bangumiapi.persons(bangumiid) or []
        if persons:
            return persons[(page - 1) * count: page * count]
        else:
            return []

    def bangumi_recommend(self, bangumiid: int) -> List[dict]:
        """
        根据BangumiID查询推荐电影
        :param bangumiid:  BangumiID
        """
        return self.bangumiapi.subjects(bangumiid) or []
