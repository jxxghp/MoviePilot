from typing import Optional, List, Tuple

from app.chain import ChainBase
from app.core.context import Context, MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas import MediaType
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
            return Context(meta=meta, mediainfo=MediaInfo(douban_info=doubaninfo))
        logger.info(f'{doubanid} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}{meta.season}')
        mediainfo.set_douban_info(doubaninfo)
        return Context(meta=meta, mediainfo=mediainfo)

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

    def douban_movies(self, sort: str, tags: str, start: int = 0, count: int = 30) -> List[MediaInfo]:
        """
        浏览豆瓣电影列表
        """
        logger.info(f'开始获取豆瓣电影列表，排序：{sort}，标签：{tags}')
        movies = self.douban_discover(mtype=MediaType.MOVIE, sort=sort, tags=tags, start=start, count=count)
        if not movies:
            logger.warn(f'豆瓣电影列表为空，排序：{sort}，标签：{tags}')
            return []
        return [MediaInfo(douban_info=movie) for movie in movies]

    def douban_tvs(self, sort: str, tags: str, start: int = 0, count: int = 30) -> List[MediaInfo]:
        """
        浏览豆瓣剧集列表
        """
        logger.info(f'开始获取豆瓣剧集列表，排序：{sort}，标签：{tags}')
        tvs = self.douban_discover(mtype=MediaType.TV, sort=sort, tags=tags, start=start, count=count)
        if not tvs:
            logger.warn(f'豆瓣剧集列表为空，排序：{sort}，标签：{tags}')
            return []
        return [MediaInfo(douban_info=tv) for tv in tvs]

    def tmdb_movies(self, sort_by: str, with_genres: str, with_original_language: str,
                    page: int = 1) -> List[MediaInfo]:
        """
        浏览TMDB电影信息
        """
        logger.info(f'开始获取TMDB电影列表，排序：{sort_by}，类型：{with_genres}，语言：{with_original_language}')
        movies = self.tmdb_discover(mtype=MediaType.MOVIE,
                                    sort_by=sort_by,
                                    with_genres=with_genres,
                                    with_original_language=with_original_language,
                                    page=page)
        if not movies:
            logger.warn(f'TMDB电影列表为空，排序：{sort_by}，类型：{with_genres}，语言：{with_original_language}')
            return []
        return [MediaInfo(tmdb_info=movie) for movie in movies]

    def tmdb_tvs(self, sort_by: str, with_genres: str, with_original_language: str,
                 page: int = 1) -> List[MediaInfo]:
        """
        浏览TMDB剧集信息
        """
        logger.info(f'开始获取TMDB剧集列表，排序：{sort_by}，类型：{with_genres}，语言：{with_original_language}')
        tvs = self.tmdb_discover(mtype=MediaType.TV,
                                 sort_by=sort_by,
                                 with_genres=with_genres,
                                 with_original_language=with_original_language,
                                 page=page)
        if not tvs:
            logger.warn(f'TMDB剧集列表为空，排序：{sort_by}，类型：{with_genres}，语言：{with_original_language}')
            return []
        return [MediaInfo(tmdb_info=tv) for tv in tvs]
