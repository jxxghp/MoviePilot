from pathlib import Path
from typing import List, Optional

from app.chain import _ChainBase
from app.core import MetaInfo, MediaInfo, settings
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
        torrents: Optional[List[dict]] = self.run_module("list_torrents", status=TorrentStatus.TRANSFER)
        if not torrents:
            logger.info("没有获取到已完成的下载任务")
            return False
        logger.info(f"获取到 {len(torrents)} 个已完成的下载任务")
        # 识别
        for torrent in torrents:
            # 识别元数据
            meta = MetaInfo(torrent.get("title"))
            if not meta.get_name():
                logger.warn(f'未识别到元数据，标题：{torrent.get("title")}')
                continue
            # 识别媒体信息
            mediainfo: MediaInfo = self.run_module('recognize_media', meta=meta)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{torrent.get("title")}')
                continue
            logger.info(f"{torrent.get('title')} 识别为：{mediainfo.type.value} {mediainfo.get_title_string()}")
            # 更新媒体图片
            self.run_module("obtain_image", mediainfo=mediainfo)
            # 转移
            dest_path: Path = self.run_module("transfer", mediainfo=mediainfo, path=torrent.get("path"))
            if not dest_path:
                logger.warn(f"{torrent.get('title')} 转移失败")
                continue
            # 转移完成
            self.run_module("transfer_completed", hashs=torrent.get("hash"))
            # 刮剥
            self.run_module("scrape_metadata", path=dest_path, mediainfo=mediainfo)
            # 移动模式删除种子
            if settings.TRANSFER_TYPE == "move":
                result = self.run_module("remove_torrents", hashs=torrent.get("hash"))
                if result:
                    logger.info(f"移动模式删除种子成功：{torrent.get('title')} ")

        logger.info("下载器文件转移执行完成")
        return True
