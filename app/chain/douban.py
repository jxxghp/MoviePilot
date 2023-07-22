from pathlib import Path
from typing import Optional, List
from typing import Union

from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import Context
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.log import logger
from app.schemas import MediaType, Notification, MessageChannel


class DoubanChain(ChainBase):
    """
    豆瓣处理链
    """

    _interests_url: str = "https://www.douban.com/feed/people/%s/interests"

    _cache_path: Path = settings.TEMP_PATH / "__doubansync_cache__"

    def __init__(self):
        super().__init__()
        self.rsshelper = RssHelper()
        self.downloadchain = DownloadChain()
        self.searchchain = SearchChain()
        self.subscribechain = SubscribeChain()

    def recognize_by_doubanid(self, doubanid: str) -> Optional[Context]:
        """
        根据豆瓣ID识别媒体信息
        """
        logger.info(f'开始识别媒体信息，豆瓣ID：{doubanid} ...')
        # 查询豆瓣信息
        doubaninfo = self.douban_info(doubanid=doubanid)
        if not doubaninfo:
            logger.warn(f'未查询到豆瓣信息，豆瓣ID：{doubanid}')
            return None
        meta = MetaInfo(title=doubaninfo.get("original_title") or doubaninfo.get("title"))
        # 识别媒体信息
        mediainfo: MediaInfo = self.recognize_media(meta=meta)
        if not mediainfo:
            logger.warn(f'{meta.name} 未识别到TMDB媒体信息')
            return Context(meta_info=meta, media_info=MediaInfo(douban_info=doubaninfo))
        logger.info(f'{doubanid} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}{meta.season}')
        mediainfo.set_douban_info(doubaninfo)
        return Context(meta_info=meta, media_info=mediainfo)

    def movie_top250(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        获取豆瓣电影TOP250
        :param page:  页码
        :param count:  每页数量
        """
        return self.run_module("movie_top250", page=page, count=count)

    def movie_showing(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        获取正在上映的电影
        """
        return self.run_module("movie_showing", page=page, count=count)

    def tv_weekly_chinese(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        获取本周中国剧集榜
        """
        return self.run_module("tv_weekly_chinese", page=page, count=count)

    def tv_weekly_global(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        获取本周全球剧集榜
        """
        return self.run_module("tv_weekly_global", page=page, count=count)

    def douban_discover(self, mtype: MediaType, sort: str, tags: str,
                        page: int = 0, count: int = 30) -> Optional[List[dict]]:
        """
        发现豆瓣电影、剧集
        :param mtype:  媒体类型
        :param sort:  排序方式
        :param tags:  标签
        :param page:  页码
        :param count:  数量
        :return: 媒体信息列表
        """
        return self.run_module("douban_discover", mtype=mtype, sort=sort, tags=tags,
                               page=page, count=count)

    def remote_sync(self, channel: MessageChannel, userid: Union[int, str]):
        """
        同步豆瓣想看数据，发送消息
        """
        self.post_message(Notification(channel=channel,
                                       title="开始同步豆瓣想看 ...", userid=userid))
        self.sync()
        self.post_message(Notification(channel=channel,
                                       title="同步豆瓣想看数据完成！", userid=userid))

    def sync(self):
        """
        通过用户RSS同步豆瓣想看数据
        """
        if not settings.DOUBAN_USER_IDS:
            return
        # 读取缓存
        caches = self._cache_path.read_text().split("\n") if self._cache_path.exists() else []
        for user_id in settings.DOUBAN_USER_IDS.split(","):
            # 同步每个用户的豆瓣数据
            if not user_id:
                continue
            logger.info(f"开始同步用户 {user_id} 的豆瓣想看数据 ...")
            url = self._interests_url % user_id
            results = self.rsshelper.parse(url)
            if not results:
                logger.error(f"未获取到用户 {user_id} 豆瓣RSS数据：{url}")
                return
            # 解析数据
            for result in results:
                dtype = result.get("title", "")[:2]
                title = result.get("title", "")[2:]
                if dtype not in ["想看"]:
                    continue
                if not result.get("link"):
                    continue
                douban_id = result.get("link", "").split("/")[-2]
                if not douban_id or douban_id in caches:
                    continue
                # 根据豆瓣ID获取豆瓣数据
                doubaninfo: Optional[dict] = self.douban_info(doubanid=douban_id)
                if not doubaninfo:
                    logger.warn(f'未获取到豆瓣信息，标题：{title}，豆瓣ID：{douban_id}')
                    continue
                logger.info(f'获取到豆瓣信息，标题：{title}，豆瓣ID：{douban_id}')
                # 识别媒体信息
                meta = MetaInfo(doubaninfo.get("original_title") or doubaninfo.get("title"))
                if doubaninfo.get("year"):
                    meta.year = doubaninfo.get("year")
                mediainfo: MediaInfo = self.recognize_media(meta=meta)
                if not mediainfo:
                    logger.warn(f'未识别到媒体信息，标题：{title}，豆瓣ID：{douban_id}')
                    continue
                # 加入缓存
                caches.append(douban_id)
                # 查询缺失的媒体信息
                exist_flag, no_exists = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                if exist_flag:
                    logger.info(f'{mediainfo.title_year} 媒体库中已存在')
                    continue
                logger.info(f'{mediainfo.title_year} 媒体库中不存在，开始搜索 ...')
                # 搜索
                contexts = self.searchchain.process(mediainfo=mediainfo,
                                                    no_exists=no_exists)
                if not contexts:
                    logger.warn(f'{mediainfo.title_year} 未搜索到资源')
                    # 添加订阅
                    self.subscribechain.add(title=mediainfo.title,
                                            year=mediainfo.year,
                                            mtype=mediainfo.type,
                                            tmdbid=mediainfo.tmdb_id,
                                            season=meta.begin_season,
                                            exist_ok=True,
                                            username="豆瓣想看")
                    continue
                # 自动下载
                downloads, lefts = self.downloadchain.batch_download(contexts=contexts, no_exists=no_exists)
                if downloads and not lefts:
                    # 全部下载完成
                    logger.info(f'{mediainfo.title_year} 下载完成')
                else:
                    # 未完成下载
                    logger.info(f'{mediainfo.title_year} 未下载未完整，添加订阅 ...')
                    # 添加订阅
                    self.subscribechain.add(title=mediainfo.title,
                                            year=mediainfo.year,
                                            mtype=mediainfo.type,
                                            tmdbid=mediainfo.tmdb_id,
                                            season=meta.begin_season,
                                            exist_ok=True,
                                            username="豆瓣想看")

            logger.info(f"用户 {user_id} 豆瓣想看同步完成")
        # 保存缓存
        self._cache_path.write_text("\n".join(caches))
