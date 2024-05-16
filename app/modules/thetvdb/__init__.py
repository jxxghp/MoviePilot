from typing import Optional, Tuple, Union

from app.core.config import settings
from app.log import logger
from app.modules import _ModuleBase
from app.modules.thetvdb import tvdbapi
from app.utils.http import RequestUtils


class TheTvDbModule(_ModuleBase):
    tvdb: tvdbapi.Tvdb = None

    def init_module(self) -> None:
        self.tvdb = tvdbapi.Tvdb(apikey=settings.TVDB_API_KEY,
                                 cache=False,
                                 select_first=True,
                                 proxies=settings.PROXY)

    @staticmethod
    def get_name() -> str:
        return "TheTvDb"

    def stop(self):
        self.tvdb.close()

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        ret = RequestUtils(proxies=settings.PROXY).get_res("https://api.thetvdb.com/series/81189")
        if ret and ret.status_code == 200:
            return True, ""
        elif ret:
            return False, f"无法连接 api.thetvdb.com，错误码：{ret.status_code}"
        return False, "api.thetvdb.com 网络连接失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def tvdb_info(self, tvdbid: int) -> Optional[dict]:
        """
        获取TVDB信息
        :param tvdbid: int
        :return: TVDB信息
        """
        try:
            logger.info(f"开始获取TVDB信息: {tvdbid} ...")
            return self.tvdb[tvdbid].data
        except Exception as err:
            logger.error(f"获取TVDB信息失败: {str(err)}")
