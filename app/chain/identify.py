from typing import Optional

from app.chain import _ChainBase
from app.core import Context, MetaInfo, MediaInfo
from app.log import logger


class IdentifyChain(_ChainBase):
    """
    识别处理链
    """

    def process(self, title: str, subtitle: str = None) -> Optional[Context]:
        """
        识别媒体信息
        """
        logger.info(f'开始识别媒体信息，标题：{title}，副标题：{subtitle} ...')
        # 识别前预处理
        result: Optional[tuple] = self.run_module('prepare_recognize', title=title, subtitle=subtitle)
        if result:
            title, subtitle = result
        # 识别元数据
        metainfo = MetaInfo(title, subtitle)
        # 识别媒体信息
        mediainfo: MediaInfo = self.run_module('recognize_media', meta=metainfo)
        if not mediainfo:
            logger.warn(f'{title} 未识别到媒体信息')
            return Context(meta=metainfo)
        logger.info(f'{title} 识别到媒体信息：{mediainfo.type.value} {mediainfo.get_title_string()}')
        # 更新媒体图片
        self.run_module('obtain_image', mediainfo=mediainfo)
        # 返回上下文
        return Context(meta=metainfo, mediainfo=mediainfo, title=title, subtitle=subtitle)
