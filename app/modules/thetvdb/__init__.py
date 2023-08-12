from typing import Optional, Tuple, Union

import tvdb_api

from app.core.config import settings
from app.log import logger
from app.modules import _ModuleBase


class TheTvDbModule(_ModuleBase):

    tvdb: tvdb_api.Tvdb = None

    def init_module(self) -> None:
        self.tvdb = tvdb_api.Tvdb(apikey=settings.TVDB_API_KEY, cache=False, select_first=True)

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
            logger.error(f"获取TVDB信息失败: {err}")
