from typing import List, Tuple, Union

from app.core import TorrentInfo
from app.modules import _ModuleBase


class FilterModule(_ModuleBase):
    def init_module(self) -> None:
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "FILTER_RULE", True

    def filter_torrents(self, torrent_list: List[TorrentInfo]) -> List[TorrentInfo]:
        """
        TODO 过滤资源
        :param torrent_list:  资源列表
        :return: 过滤后的资源列表，注意如果返回None，有可能是没有对应的处理模块，应无视结果
        """
        pass
