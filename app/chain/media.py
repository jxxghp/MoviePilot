from typing import Optional, List, Tuple

from app.chain import ChainBase
from app.core.context import Context, MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.log import logger
from app.utils.string import StringUtils


class MediaChain(ChainBase):
    """
    媒体信息处理链
    """

    def recognize_by_title(self, title: str, subtitle: str = None) -> Optional[Context]:
        """
        根据主副标题识别媒体信息
        """
        logger.info(f'开始识别媒体信息，标题：{title}，副标题：{subtitle} ...')
        # 识别前预处理
        result: Optional[tuple] = self.prepare_recognize(title=title, subtitle=subtitle)
        if result:
            title, subtitle = result
        # 识别元数据
        metainfo = MetaInfo(title, subtitle)
        # 识别媒体信息
        mediainfo: MediaInfo = self.recognize_media(meta=metainfo)
        if not mediainfo:
            logger.warn(f'{title} 未识别到媒体信息')
            return Context(meta=metainfo)
        logger.info(f'{title} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}')
        # 更新媒体图片
        self.obtain_image(mediainfo=mediainfo)
        # 返回上下文
        return Context(meta=metainfo, mediainfo=mediainfo, title=title, subtitle=subtitle)

    def search(self, title: str) -> Tuple[MetaBase, List[MediaInfo]]:
        """
        搜索媒体信息
        :param title: 搜索内容
        :return: 识别元数据，媒体信息列表
        """
        # 提取要素
        mtype, key_word, season_num, episode_num, year, content = StringUtils.get_keyword(title)
        # 识别
        meta = MetaInfo(content)
        if not meta.name:
            logger.warn(f'{title} 未识别到元数据！')
            return meta, []
        # 合并信息
        if mtype:
            meta.type = mtype
        if season_num:
            meta.begin_season = season_num
        if episode_num:
            meta.begin_episode = episode_num
        if year:
            meta.year = year
        # 开始搜索
        logger.info(f"开始搜索媒体信息：{meta.name}")
        medias: Optional[List[MediaInfo]] = self.search_medias(meta=meta)
        if not medias:
            logger.warn(f"{meta.name} 没有找到对应的媒体信息！")
            return meta, []
        logger.info(f"{content} 搜索到 {len(medias)} 条相关媒体信息")
        # 识别的元数据，媒体信息列表
        return meta, medias
