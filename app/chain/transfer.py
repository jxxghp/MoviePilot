from typing import List, Optional

from app.chain import _ChainBase
from app.core import MetaInfo, MediaInfo
from app.log import logger
from app.utils.types import TorrentStatus


class TransferChain(_ChainBase):
    """
    文件转移处理链
    """

    def process(self) -> bool:
        """
        获取下载器中的种子列表，并执行转移
        """
        logger.info("开始执行下载器文件转移 ...")
        # 从下载器获取种子列表
        torrents: Optional[List[dict]] = self.run_module("list_torrents", status=TorrentStatus.COMPLETE)
        if not torrents:
            logger.info("没有获取到已完成的下载任务")
            return False
        logger.info(f"获取到 {len(torrents)} 个已完成的下载任务")
        # 识别
        for torrent in torrents:
            # 识别元数据
            meta = MetaInfo(torrent.get("title"))
            # 识别媒体信息
            mediainfo: MediaInfo = self.run_module('recognize_media', meta=meta)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{torrent.get("title")}')
                return False
            logger.info(f"{torrent.get('title')} 识别为：{mediainfo.type.value} {mediainfo.get_title_string()}")
            # 更新媒体图片
            self.run_module("obtain_image", mediainfo=mediainfo)
            # 转移
            self.run_module("transfer", mediainfo=mediainfo, torrent=torrent)
        # 转移
        pass
