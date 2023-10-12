import copy
import time
from pathlib import Path
from threading import Lock
from typing import Optional, List, Tuple

from app.chain import ChainBase
from app.core.context import Context, MediaInfo
from app.core.event import eventmanager, Event
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo, MetaInfoPath
from app.log import logger
from app.schemas.types import EventType, MediaType
from app.utils.singleton import Singleton
from app.utils.string import StringUtils


recognize_lock = Lock()


class MediaChain(ChainBase, metaclass=Singleton):
    """
    媒体信息处理链，单例运行
    """
    # 临时识别标题
    recognize_title: Optional[str] = None
    # 临时识别结果 {title, name, year, season, episode}
    recognize_temp: Optional[dict] = None

    def recognize_by_title(self, title: str, subtitle: str = None) -> Optional[Context]:
        """
        根据主副标题识别媒体信息
        """
        logger.info(f'开始识别媒体信息，标题：{title}，副标题：{subtitle} ...')
        # 识别元数据
        metainfo = MetaInfo(title, subtitle)
        # 识别媒体信息
        mediainfo: MediaInfo = self.recognize_media(meta=metainfo)
        if not mediainfo:
            # 偿试使用辅助识别，如果有注册响应事件的话
            if eventmanager.check(EventType.NameRecognize):
                logger.info(f'请求辅助识别，标题：{title} ...')
                mediainfo = self.recognize_help(title=title, org_meta=metainfo)
            if not mediainfo:
                logger.warn(f'{title} 未识别到媒体信息')
                return Context(meta_info=metainfo)
        # 识别成功
        logger.info(f'{title} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}')
        # 更新媒体图片
        self.obtain_images(mediainfo=mediainfo)
        # 返回上下文
        return Context(meta_info=metainfo, media_info=mediainfo)

    def recognize_help(self, title: str, org_meta: MetaBase) -> Optional[MediaInfo]:
        """
        请求辅助识别，返回媒体信息
        :param title: 标题
        :param org_meta: 原始元数据
        """
        with recognize_lock:
            self.recognize_temp = None
            self.recognize_title = title

        # 发送请求事件
        eventmanager.send_event(
            EventType.NameRecognize,
            {
                'title': title,
            }
        )
        # 每0.5秒循环一次，等待结果，直到10秒后超时
        for i in range(10):
            if self.recognize_temp is not None:
                break
            time.sleep(0.5)
        # 加锁
        with recognize_lock:
            mediainfo = None
            if not self.recognize_temp or self.recognize_title != title:
                # 没有识别结果或者识别标题已改变
                return None
            # 有识别结果
            meta_dict = copy.deepcopy(self.recognize_temp)
        logger.info(f'获取到辅助识别结果：{meta_dict}')
        if meta_dict.get("name") == org_meta.name and meta_dict.get("year") == org_meta.year:
            logger.info(f'辅助识别结果与原始识别结果一致')
        else:
            logger.info(f'辅助识别结果与原始识别结果不一致，重新匹配媒体信息 ...')
            org_meta.name = meta_dict.get("name")
            org_meta.year = meta_dict.get("year")
            org_meta.begin_season = meta_dict.get("season")
            org_meta.begin_episode = meta_dict.get("episode")
            if org_meta.begin_season or org_meta.begin_episode:
                org_meta.type = MediaType.TV
            # 重新识别
            mediainfo = self.recognize_media(meta=org_meta)
        return mediainfo

    @eventmanager.register(EventType.NameRecognizeResult)
    def recognize_result(self, event: Event):
        """
        监控识别结果事件，获取辅助识别结果，结果格式：{title, name, year, season, episode}
        """
        if not event:
            return
        event_data = event.event_data or {}
        # 加锁
        with recognize_lock:
            # 不是原标题的结果不要
            if event_data.get("title") != self.recognize_title:
                return
            # 标志收到返回
            self.recognize_temp = {}
            # 处理数据格式
            file_title, file_year, season_number, episode_number = None, None, None, None
            if event_data.get("name"):
                file_title = str(event_data["name"]).split("/")[0].strip().replace(".", " ")
            if event_data.get("year"):
                file_year = str(event_data["year"]).split("/")[0].strip()
            if event_data.get("season") and str(event_data["season"]).isdigit():
                season_number = int(event_data["season"])
            if event_data.get("episode") and str(event_data["episode"]).isdigit():
                episode_number = int(event_data["episode"])
            if not file_title:
                return
            if file_title == 'Unknown':
                return
            if not str(file_year).isdigit():
                file_year = None
            # 结果赋值
            self.recognize_temp = {
                "name": file_title,
                "year": file_year,
                "season": season_number,
                "episode": episode_number
            }

    def recognize_by_path(self, path: str) -> Optional[Context]:
        """
        根据文件路径识别媒体信息
        """
        logger.info(f'开始识别媒体信息，文件：{path} ...')
        file_path = Path(path)
        # 元数据
        file_meta = MetaInfoPath(file_path)
        # 识别媒体信息
        mediainfo = self.recognize_media(meta=file_meta)
        if not mediainfo:
            # 偿试使用辅助识别，如果有注册响应事件的话
            if eventmanager.check(EventType.NameRecognize):
                logger.info(f'请求辅助识别，标题：{file_path.name} ...')
                mediainfo = self.recognize_help(title=path, org_meta=file_meta)
            if not mediainfo:
                logger.warn(f'{path} 未识别到媒体信息')
                return Context(meta_info=file_meta)
        logger.info(f'{path} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}')
        # 更新媒体图片
        self.obtain_images(mediainfo=mediainfo)
        # 返回上下文
        return Context(meta_info=file_meta, media_info=mediainfo)

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
