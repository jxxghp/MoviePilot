"""
TVDB 电视剧身份查询的模块调度入口。
"""

from typing import List

from app.chain.base import ChainBase


class TvdbChain(ChainBase):
    """
    Tvdb处理链，单例运行
    """

    def get_tvdbid_by_name(self, title: str) -> List[int]:
        """
        按电视剧名称查询匹配的 TVDB 标识列表。

        :param title: 用于电视剧搜索的标题
        :return: 搜索结果中的 TVDB 整数标识，按模块返回顺序排列
        """
        tvdb_info_list = self.run_module("search_tvdb", title=title)
        return [int(item["tvdb_id"]) for item in tvdb_info_list]
