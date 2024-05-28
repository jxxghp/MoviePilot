import json
import threading
from typing import List, Union, Optional

from app import schemas
from app.chain import ChainBase
from app.core.config import settings
from app.db.mediaserver_oper import MediaServerOper
from app.log import logger

lock = threading.Lock()


class MediaServerChain(ChainBase):
    """
    媒体服务器处理链
    """

    def __init__(self):
        super().__init__()
        self.dboper = MediaServerOper()

    def librarys(self, server: str = None, username: str = None) -> List[schemas.MediaServerLibrary]:
        """
        获取媒体服务器所有媒体库
        """
        return self.run_module("mediaserver_librarys", server=server, username=username)

    def items(self, server: str, library_id: Union[str, int]) -> List[schemas.MediaServerItem]:
        """
        获取媒体服务器所有项目
        """
        return self.run_module("mediaserver_items", server=server, library_id=library_id)

    def iteminfo(self, server: str, item_id: Union[str, int]) -> schemas.MediaServerItem:
        """
        获取媒体服务器项目信息
        """
        return self.run_module("mediaserver_iteminfo", server=server, item_id=item_id)

    def episodes(self, server: str, item_id: Union[str, int]) -> List[schemas.MediaServerSeasonInfo]:
        """
        获取媒体服务器剧集信息
        """
        return self.run_module("mediaserver_tv_episodes", server=server, item_id=item_id)

    def playing(self, count: int = 20, server: str = None, username: str = None) -> List[schemas.MediaServerPlayItem]:
        """
        获取媒体服务器正在播放信息
        """
        return self.run_module("mediaserver_playing", count=count, server=server, username=username)

    def latest(self, count: int = 20, server: str = None, username: str = None) -> List[schemas.MediaServerPlayItem]:
        """
        获取媒体服务器最新入库条目
        """
        return self.run_module("mediaserver_latest", count=count, server=server, username=username)

    def get_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """
        获取播放地址
        """
        return self.run_module("mediaserver_play_url", server=server, item_id=item_id)

    def sync(self):
        """
        同步媒体库所有数据到本地数据库
        """
        # 设置的媒体服务器
        if not settings.MEDIASERVER:
            return
        # 同步黑名单
        sync_blacklist = settings.MEDIASERVER_SYNC_BLACKLIST.split(
            ",") if settings.MEDIASERVER_SYNC_BLACKLIST else []
        mediaservers = settings.MEDIASERVER.split(",")
        with lock:
            # 汇总统计
            total_count = 0
            # 清空登记薄
            self.dboper.empty()
            # 遍历媒体服务器
            for mediaserver in mediaservers:
                if not mediaserver:
                    continue
                logger.info(f"开始同步媒体库 {mediaserver} 的数据 ...")
                for library in self.librarys(mediaserver):
                    # 同步黑名单 跳过
                    if library.name in sync_blacklist:
                        continue
                    logger.info(f"正在同步 {mediaserver} 媒体库 {library.name} ...")
                    library_count = 0
                    for item in self.items(mediaserver, library.id):
                        if not item:
                            continue
                        if not item.item_id:
                            continue
                        logger.debug(f"正在同步 {item.title} ...")
                        # 计数
                        library_count += 1
                        seasoninfo = {}
                        # 类型
                        item_type = "电视剧" if item.item_type in ['Series', 'show'] else "电影"
                        if item_type == "电视剧":
                            # 查询剧集信息
                            espisodes_info = self.episodes(mediaserver, item.item_id) or []
                            for episode in espisodes_info:
                                seasoninfo[episode.season] = episode.episodes
                        # 插入数据
                        item_dict = item.dict()
                        item_dict['seasoninfo'] = json.dumps(seasoninfo)
                        item_dict['item_type'] = item_type
                        self.dboper.add(**item_dict)
                    logger.info(f"{mediaserver} 媒体库 {library.name} 同步完成，共同步数量：{library_count}")
                    # 总数累加
                    total_count += library_count
            logger.info("【MediaServer】媒体库数据同步完成，同步数量：%s" % total_count)
