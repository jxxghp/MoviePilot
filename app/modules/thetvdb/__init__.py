from typing import Optional, Tuple, Union


from app.core.config import settings
from app.log import logger
from app.modules import _ModuleBase
from app.modules.thetvdb import tvdbapi


class TheTvDbModule(_ModuleBase):

    tvdb: tvdbapi.Tvdb = None

    def init_module(self) -> None:
        self.tvdb = tvdbapi.Tvdb(apikey=settings.TVDB_API_KEY,
                                 cache=False,
                                 select_first=True,
                                 proxies=settings.PROXY)

    def stop(self):
        pass

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
