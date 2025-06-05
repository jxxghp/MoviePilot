from typing import List

from app.chain import ChainBase


class TvdbChain(ChainBase):
    """
    Tvdb处理链，单例运行
    """

    def get_tvdbid_by_name(self, title: str) -> List[int]:
        tvdb_info_list = self.run_module("search_tvdb", title=title)
        return [int(item["tvdb_id"]) for item in tvdb_info_list]
