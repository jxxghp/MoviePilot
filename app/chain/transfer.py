from typing import List, Optional

from app.chain import ChainBase
from app.core.metainfo import MetaInfo
from app.core.context import MediaInfo
from app.core.config import settings
from app.core.meta import MetaBase
from app.log import logger
from app.utils.string import StringUtils
from app.utils.system import SystemUtils
from app.utils.types import TorrentStatus, MediaType


class TransferChain(ChainBase):
    """
    文件转移处理链
    """

    def process(self) -> bool:
        """
        获取下载器中的种子列表，并执行转移
        """
        logger.info("开始执行下载器文件转移 ...")
        # 从下载器获取种子列表
        torrents: Optional[List[dict]] = self.list_torrents(status=TorrentStatus.TRANSFER)
        if not torrents:
            logger.info("没有获取到已完成的下载任务")
            return False
        logger.info(f"获取到 {len(torrents)} 个已完成的下载任务")
        # 识别
        for torrent in torrents:
            # 识别元数据
            meta: MetaBase = MetaInfo(torrent.get("title"))
            if not meta.get_name():
                logger.warn(f'未识别到元数据，标题：{torrent.get("title")}')
                continue
            # 识别媒体信息
            mediainfo: MediaInfo = self.recognize_media(meta=meta)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{torrent.get("title")}')
                self.post_message(title=f"{torrent.get('title')} 未识别到媒体信息，无法入库！")
                continue
            logger.info(f"{torrent.get('title')} 识别为：{mediainfo.type.value} {mediainfo.get_title_string()}")
            # 更新媒体图片
            self.obtain_image(mediainfo=mediainfo)
            # 转移
            transferinfo: dict = self.transfer(mediainfo=mediainfo, path=torrent.get("path"))
            if not transferinfo or not transferinfo.get("target_path"):
                logger.warn(f"{torrent.get('title')} 入库失败")
                self.post_message(
                    title=f"{mediainfo.get_title_string()}{meta.get_season_episode_string()} 入库失败！",
                    text=f"原因：{transferinfo.get('message') if transferinfo else '未知'}\n"
                         f"路径：{torrent.get('path')}",
                    image=mediainfo.get_message_image()
                ),
                continue
            # 转移完成
            self.transfer_completed(hashs=torrent.get("hash"))
            # 刮剥
            self.scrape_metadata(path=transferinfo.get('target_path'), mediainfo=mediainfo)
            # 移动模式删除种子
            if settings.TRANSFER_TYPE == "move":
                result = self.remove_torrents(hashs=torrent.get("hash"))
                if result:
                    logger.info(f"移动模式删除种子成功：{torrent.get('title')} ")
            # 发送通知
            self.__send_transfer_message(meta=meta, mediainfo=mediainfo, transferinfo=transferinfo)

        logger.info("下载器文件转移执行完成")
        return True

    def __send_transfer_message(self, meta: MetaBase, mediainfo: MediaInfo, transferinfo: dict):
        """
        发送入库成功的消息
        """
        # 文件大小
        file_size = StringUtils.str_filesize(
            SystemUtils.get_directory_size(
                transferinfo.get('target_path')
            )
        )
        msg_title = f"{mediainfo.get_title_string()} 已入库"
        if mediainfo.vote_average:
            msg_str = f"评分：{mediainfo.vote_average}，类型：{mediainfo.type.value}"
        else:
            msg_str = f"类型：{mediainfo.type.value}"
        if mediainfo.category:
            msg_str = f"{msg_str}，类别：{mediainfo.category}"
        if meta.get_resource_type_string():
            msg_str = f"{msg_str}，质量：{meta.get_resource_type_string()}"
        msg_str = f"{msg_str}， 大小：{file_size}"
        # 发送
        self.post_message(title=msg_title, text=msg_str, image=mediainfo.get_message_image())
