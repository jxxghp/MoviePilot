from pathlib import Path
from typing import Optional

from app.chain import ChainBase
from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.core.context import MediaInfo
from app.helper.rss import RssHelper
from app.log import logger


class DoubanSyncChain(ChainBase):
    """
    同步豆瓣想看数据
    """

    _interests_url: str = "https://www.douban.com/feed/people/%s/interests"

    _cache_path: Path = settings.TEMP_PATH / "__doubansync_cache__"

    def __init__(self):
        super().__init__()
        self.rsshelper = RssHelper()
        self.downloadchain = DownloadChain()
        self.searchchain = SearchChain()
        self.subscribechain = SubscribeChain()

    def process(self):
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
                    logger.info(f'{mediainfo.get_title_string()} 媒体库中已存在')
                    continue
                logger.info(f'{mediainfo.get_title_string()} 媒体库中不存在，开始搜索 ...')
                # 搜索
                contexts = self.searchchain.process(meta=meta,
                                                    mediainfo=mediainfo,
                                                    no_exists=no_exists)
                if not contexts:
                    logger.warn(f'{mediainfo.get_title_string()} 未搜索到资源')
                    continue
                # 自动下载
                downloads, lefts = self.downloadchain.batch_download(contexts=contexts, need_tvs=no_exists)
                if downloads and not lefts:
                    # 全部下载完成
                    logger.info(f'{mediainfo.get_title_string()} 下载完成')
                else:
                    # 未完成下载
                    logger.info(f'{mediainfo.get_title_string()} 未下载未完整，添加订阅 ...')
                    # 添加订阅
                    self.subscribechain.process(title=mediainfo.title,
                                                year=mediainfo.year,
                                                mtype=mediainfo.type,
                                                tmdbid=mediainfo.tmdb_id,
                                                season=meta.begin_season,
                                                username="豆瓣想看")

            logger.info(f"用户 {user_id} 豆瓣想看同步完成")
        # 保存缓存
        self._cache_path.write_text("\n".join(caches))
