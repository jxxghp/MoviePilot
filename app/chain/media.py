from pathlib import Path
from threading import Lock
from typing import Optional, List, Tuple, Union

from app import schemas
from app.chain import ChainBase
from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.context import Context, MediaInfo
from app.core.event import eventmanager, Event
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo, MetaInfoPath
from app.log import logger
from app.schemas import FileItem
from app.schemas.types import EventType, MediaType, ChainEventType
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.string import StringUtils

recognize_lock = Lock()
scraping_lock = Lock()
scraping_files = []


class MediaChain(ChainBase, metaclass=Singleton):
    """
    媒体信息处理链，单例运行
    """

    def __init__(self):
        super().__init__()
        self.storagechain = StorageChain()

    def metadata_nfo(self, meta: MetaBase, mediainfo: MediaInfo,
                     season: Optional[int] = None, episode: Optional[int] = None) -> Optional[str]:
        """
        获取NFO文件内容文本
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        return self.run_module("metadata_nfo", meta=meta, mediainfo=mediainfo, season=season, episode=episode)

    def recognize_by_meta(self, metainfo: MetaBase, episode_group: Optional[str] = None) -> Optional[MediaInfo]:
        """
        根据主副标题识别媒体信息
        """
        title = metainfo.title
        # 识别媒体信息
        mediainfo: MediaInfo = self.recognize_media(meta=metainfo, episode_group=episode_group)
        if not mediainfo:
            # 尝试使用辅助识别，如果有注册响应事件的话
            if eventmanager.check(ChainEventType.NameRecognize):
                logger.info(f'请求辅助识别，标题：{title} ...')
                mediainfo = self.recognize_help(title=title, org_meta=metainfo)
            if not mediainfo:
                logger.warn(f'{title} 未识别到媒体信息')
                return None
        # 识别成功
        logger.info(f'{title} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}')
        # 更新媒体图片
        self.obtain_images(mediainfo=mediainfo)
        # 返回上下文
        return mediainfo

    def recognize_help(self, title: str, org_meta: MetaBase) -> Optional[MediaInfo]:
        """
        请求辅助识别，返回媒体信息
        :param title: 标题
        :param org_meta: 原始元数据
        """
        # 发送请求事件，等待结果
        result: Event = eventmanager.send_event(
            ChainEventType.NameRecognize,
            {
                'title': title,
            }
        )
        if not result:
            return None
        # 获取返回事件数据
        event_data = result.event_data or {}
        logger.info(f'获取到辅助识别结果：{event_data}')
        # 处理数据格式
        title, year, season_number, episode_number = None, None, None, None
        if event_data.get("name"):
            title = str(event_data["name"]).split("/")[0].strip().replace(".", " ")
        if event_data.get("year"):
            year = str(event_data["year"]).split("/")[0].strip()
        if event_data.get("season") and str(event_data["season"]).isdigit():
            season_number = int(event_data["season"])
        if event_data.get("episode") and str(event_data["episode"]).isdigit():
            episode_number = int(event_data["episode"])
        if not title:
            return None
        if title == 'Unknown':
            return None
        if not str(year).isdigit():
            year = None
        # 结果赋值
        if title == org_meta.name and year == org_meta.year:
            logger.info(f'辅助识别与原始识别结果一致，无需重新识别媒体信息')
            return None
        logger.info(f'辅助识别结果与原始识别结果不一致，重新匹配媒体信息 ...')
        org_meta.name = title
        org_meta.year = year
        org_meta.begin_season = season_number
        org_meta.begin_episode = episode_number
        if org_meta.begin_season or org_meta.begin_episode:
            org_meta.type = MediaType.TV
        # 重新识别
        return self.recognize_media(meta=org_meta)

    def recognize_by_path(self, path: str, episode_group: Optional[str] = None) -> Optional[Context]:
        """
        根据文件路径识别媒体信息
        """
        logger.info(f'开始识别媒体信息，文件：{path} ...')
        file_path = Path(path)
        # 元数据
        file_meta = MetaInfoPath(file_path)
        # 识别媒体信息
        mediainfo = self.recognize_media(meta=file_meta, episode_group=episode_group)
        if not mediainfo:
            # 尝试使用辅助识别，如果有注册响应事件的话
            if eventmanager.check(ChainEventType.NameRecognize):
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

    def search(self, title: str) -> Tuple[Optional[MetaBase], List[MediaInfo]]:
        """
        搜索媒体/人物信息
        :param title: 搜索内容
        :return: 识别元数据，媒体信息列表
        """
        # 提取要素
        mtype, key_word, season_num, episode_num, year, content = StringUtils.get_keyword(title)
        # 识别
        meta = MetaInfo(content)
        if not meta.name:
            meta.cn_name = content
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

    def get_tmdbinfo_by_doubanid(self, doubanid: str, mtype: MediaType = None) -> Optional[dict]:
        """
        根据豆瓣ID获取TMDB信息
        """
        tmdbinfo = None
        doubaninfo = self.douban_info(doubanid=doubanid, mtype=mtype)
        if doubaninfo:
            # 优先使用原标题匹配
            if doubaninfo.get("original_title"):
                meta = MetaInfo(title=doubaninfo.get("title"))
                meta_org = MetaInfo(title=doubaninfo.get("original_title"))
            else:
                meta_org = meta = MetaInfo(title=doubaninfo.get("title"))
            # 年份
            if doubaninfo.get("year"):
                meta.year = doubaninfo.get("year")
            # 处理类型
            if isinstance(doubaninfo.get('media_type'), MediaType):
                meta.type = doubaninfo.get('media_type')
            else:
                meta.type = MediaType.MOVIE if doubaninfo.get("type") == "movie" else MediaType.TV
            # 匹配TMDB信息
            meta_names = list(dict.fromkeys([k for k in [meta_org.name,
                                                         meta.cn_name,
                                                         meta.en_name] if k]))
            for name in meta_names:
                tmdbinfo = self.match_tmdbinfo(
                    name=name,
                    year=meta.year,
                    mtype=mtype or meta.type,
                    season=meta.begin_season
                )
                if tmdbinfo:
                    # 合季季后返回
                    tmdbinfo['season'] = meta.begin_season
                    break
        return tmdbinfo

    def get_tmdbinfo_by_bangumiid(self, bangumiid: int) -> Optional[dict]:
        """
        根据BangumiID获取TMDB信息
        """
        bangumiinfo = self.bangumi_info(bangumiid=bangumiid)
        if bangumiinfo:
            # 优先使用原标题匹配
            if bangumiinfo.get("name_cn"):
                meta = MetaInfo(title=bangumiinfo.get("name"))
                meta_cn = MetaInfo(title=bangumiinfo.get("name_cn"))
            else:
                meta_cn = meta = MetaInfo(title=bangumiinfo.get("name"))
            # 年份
            release_date = bangumiinfo.get("date") or bangumiinfo.get("air_date")
            if release_date:
                year = release_date[:4]
            else:
                year = None
            # 识别TMDB媒体信息
            meta_names = list(dict.fromkeys([k for k in [meta_cn.name,
                                                         meta.name] if k]))
            for name in meta_names:
                tmdbinfo = self.match_tmdbinfo(
                    name=name,
                    year=year,
                    mtype=MediaType.TV,
                    season=meta.begin_season
                )
                if tmdbinfo:
                    return tmdbinfo
        return None

    def get_doubaninfo_by_tmdbid(self, tmdbid: int,
                                 mtype: MediaType = None, season: Optional[int] = None) -> Optional[dict]:
        """
        根据TMDBID获取豆瓣信息
        """
        tmdbinfo = self.tmdb_info(tmdbid=tmdbid, mtype=mtype)
        if tmdbinfo:
            # 名称
            name = tmdbinfo.get("title") or tmdbinfo.get("name")
            # 年份
            year = None
            if tmdbinfo.get('release_date'):
                year = tmdbinfo['release_date'][:4]
            elif tmdbinfo.get('seasons') and season:
                for seainfo in tmdbinfo['seasons']:
                    # 季
                    season_number = seainfo.get("season_number")
                    if not season_number:
                        continue
                    air_date = seainfo.get("air_date")
                    if air_date and season_number == season:
                        year = air_date[:4]
                        break
            # IMDBID
            imdbid = tmdbinfo.get("external_ids", {}).get("imdb_id")
            return self.match_doubaninfo(
                name=name,
                year=year,
                mtype=mtype,
                imdbid=imdbid
            )
        return None

    def get_doubaninfo_by_bangumiid(self, bangumiid: int) -> Optional[dict]:
        """
        根据BangumiID获取豆瓣信息
        """
        bangumiinfo = self.bangumi_info(bangumiid=bangumiid)
        if bangumiinfo:
            # 优先使用中文标题匹配
            if bangumiinfo.get("name_cn"):
                meta = MetaInfo(title=bangumiinfo.get("name_cn"))
            else:
                meta = MetaInfo(title=bangumiinfo.get("name"))
            # 年份
            release_date = bangumiinfo.get("date") or bangumiinfo.get("air_date")
            if release_date:
                year = release_date[:4]
            else:
                year = None
            # 使用名称识别豆瓣媒体信息
            return self.match_doubaninfo(
                name=meta.name,
                year=year,
                mtype=MediaType.TV,
                season=meta.begin_season
            )
        return None

    @eventmanager.register(EventType.MetadataScrape)
    def scrape_metadata_event(self, event: Event):
        """
        监控手动刮削事件
        """
        if not event:
            return
        event_data = event.event_data or {}
        fileitem: FileItem = event_data.get("fileitem")
        meta: MetaBase = event_data.get("meta")
        mediainfo: MediaInfo = event_data.get("mediainfo")
        overwrite = event_data.get("overwrite", False)
        if not fileitem:
            return
        # 刮削锁
        with scraping_lock:
            if fileitem.path in scraping_files:
                return
            scraping_files.append(fileitem.path)
        try:
            # 执行刮削
            self.scrape_metadata(fileitem=fileitem, meta=meta, mediainfo=mediainfo, overwrite=overwrite)
        finally:
            # 释放锁
            with scraping_lock:
                scraping_files.remove(fileitem.path)

    def scrape_metadata(self, fileitem: schemas.FileItem,
                        meta: MetaBase = None, mediainfo: MediaInfo = None,
                        init_folder: bool = True, parent: schemas.FileItem = None,
                        overwrite: bool = False):
        """
        手动刮削媒体信息
        :param fileitem: 刮削目录或文件
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param init_folder: 是否刮削根目录
        :param parent: 上级目录
        :param overwrite: 是否覆盖已有文件
        """

        def is_bluray_folder(_fileitem: schemas.FileItem) -> bool:
            """
            判断是否为原盘目录
            """
            if not _fileitem or _fileitem.type != "dir":
                return False
            # 蓝光原盘目录必备的文件或文件夹
            required_files = ['BDMV', 'CERTIFICATE']
            # 检查目录下是否存在所需文件或文件夹
            for item in self.storagechain.list_files(_fileitem):
                if item.name in required_files:
                    return True
            return False

        def __list_files(_fileitem: schemas.FileItem):
            """
            列出下级文件
            """
            return self.storagechain.list_files(fileitem=_fileitem)

        def __save_file(_fileitem: schemas.FileItem, _path: Path, _content: Union[bytes, str]):
            """
            保存或上传文件
            :param _fileitem: 关联的媒体文件项
            :param _path: 元数据文件路径
            :param _content: 文件内容
            """
            if not _fileitem or not _content or not _path:
                return
            # 保存文件到临时目录，文件名随机
            tmp_file = settings.TEMP_PATH / f"{_path.name}.{StringUtils.generate_random_str(10)}"
            tmp_file.write_bytes(_content)
            # 获取文件的父目录
            try:
                item = self.storagechain.upload_file(fileitem=_fileitem, path=tmp_file, new_name=_path.name)
                if item:
                    logger.info(f"已保存文件：{item.path}")
                else:
                    logger.warn(f"文件保存失败：{_path}")
            finally:
                if tmp_file.exists():
                    tmp_file.unlink()

        def __download_image(_url: str) -> Optional[bytes]:
            """
            下载图片并保存
            """
            try:
                logger.info(f"正在下载图片：{_url} ...")
                r = RequestUtils(proxies=settings.PROXY).get_res(url=_url)
                if r:
                    return r.content
                else:
                    logger.info(f"{_url} 图片下载失败，请检查网络连通性！")
            except Exception as err:
                logger.error(f"{_url} 图片下载失败：{str(err)}！")
            return None

        # 当前文件路径
        filepath = Path(fileitem.path)
        if fileitem.type == "file" \
                and (not filepath.suffix or filepath.suffix.lower() not in settings.RMT_MEDIAEXT):
            return
        if not meta:
            meta = MetaInfoPath(filepath)
        if not mediainfo:
            mediainfo = self.recognize_by_meta(meta)
        if not mediainfo:
            logger.warn(f"{filepath} 无法识别文件媒体信息！")
            return
        logger.info(f"开始刮削：{filepath} ...")
        if mediainfo.type == MediaType.MOVIE:
            # 电影
            if fileitem.type == "file":
                # 是否已存在
                nfo_path = filepath.with_suffix(".nfo")
                if overwrite or not self.storagechain.get_file_item(storage=fileitem.storage, path=nfo_path):
                    # 电影文件
                    movie_nfo = self.metadata_nfo(meta=meta, mediainfo=mediainfo)
                    if movie_nfo:
                        # 保存或上传nfo文件到上级目录
                        __save_file(_fileitem=parent, _path=nfo_path, _content=movie_nfo)
                    else:
                        logger.warn(f"{filepath.name} nfo文件生成失败！")
                else:
                    logger.info(f"已存在nfo文件：{nfo_path}")
            else:
                # 电影目录
                if is_bluray_folder(fileitem):
                    # 原盘目录
                    nfo_path = filepath / (filepath.name + ".nfo")
                    if overwrite or not self.storagechain.get_file_item(storage=fileitem.storage, path=nfo_path):
                        # 生成原盘nfo
                        movie_nfo = self.metadata_nfo(meta=meta, mediainfo=mediainfo)
                        if movie_nfo:
                            # 保存或上传nfo文件到当前目录
                            __save_file(_fileitem=fileitem, _path=nfo_path, _content=movie_nfo)
                        else:
                            logger.warn(f"{filepath.name} nfo文件生成失败！")
                    else:
                        logger.info(f"已存在nfo文件：{nfo_path}")
                else:
                    # 处理目录内的文件
                    files = __list_files(_fileitem=fileitem)
                    for file in files:
                        self.scrape_metadata(fileitem=file,
                                             meta=meta, mediainfo=mediainfo,
                                             init_folder=False, parent=fileitem,
                                             overwrite=overwrite)
                # 生成目录内图片文件
                if init_folder:
                    # 图片
                    for attr_name, attr_value in vars(mediainfo).items():
                        if attr_value \
                                and attr_name.endswith("_path") \
                                and attr_value \
                                and isinstance(attr_value, str) \
                                and attr_value.startswith("http"):
                            image_name = attr_name.replace("_path", "") + Path(attr_value).suffix
                            image_path = filepath / image_name
                            if overwrite or not self.storagechain.get_file_item(storage=fileitem.storage,
                                                                                path=image_path):
                                # 下载图片
                                content = __download_image(_url=attr_value)
                                # 写入图片到当前目录
                                if content:
                                    __save_file(_fileitem=fileitem, _path=image_path, _content=content)
                            else:
                                logger.info(f"已存在图片文件：{image_path}")
        else:
            # 电视剧
            if fileitem.type == "file":
                # 重新识别季集
                file_meta = MetaInfoPath(filepath)
                if not file_meta.begin_episode:
                    logger.warn(f"{filepath.name} 无法识别文件集数！")
                    return
                file_mediainfo = self.recognize_media(meta=file_meta, tmdbid=mediainfo.tmdb_id,
                                                      episode_group=mediainfo.episode_group)
                if not file_mediainfo:
                    logger.warn(f"{filepath.name} 无法识别文件媒体信息！")
                    return
                # 是否已存在
                nfo_path = filepath.with_suffix(".nfo")
                if overwrite or not self.storagechain.get_file_item(storage=fileitem.storage, path=nfo_path):
                    # 获取集的nfo文件
                    episode_nfo = self.metadata_nfo(meta=file_meta, mediainfo=file_mediainfo,
                                                    season=file_meta.begin_season,
                                                    episode=file_meta.begin_episode)
                    if episode_nfo:
                        # 保存或上传nfo文件到上级目录
                        if not parent:
                            parent = self.storagechain.get_parent_item(fileitem)
                        __save_file(_fileitem=parent, _path=nfo_path, _content=episode_nfo)
                    else:
                        logger.warn(f"{filepath.name} nfo文件生成失败！")
                else:
                    logger.info(f"已存在nfo文件：{nfo_path}")
                # 获取集的图片
                image_dict = self.metadata_img(mediainfo=file_mediainfo,
                                               season=file_meta.begin_season, episode=file_meta.begin_episode)
                if image_dict:
                    for episode, image_url in image_dict.items():
                        image_path = filepath.with_suffix(Path(image_url).suffix)
                        if overwrite or not self.storagechain.get_file_item(storage=fileitem.storage, path=image_path):
                            # 下载图片
                            content = __download_image(image_url)
                            # 保存图片文件到当前目录
                            if content:
                                if not parent:
                                    parent = self.storagechain.get_parent_item(fileitem)
                                __save_file(_fileitem=parent, _path=image_path, _content=content)
                        else:
                            logger.info(f"已存在图片文件：{image_path}")
            else:
                # 当前为目录，处理目录内的文件
                files = __list_files(_fileitem=fileitem)
                for file in files:
                    self.scrape_metadata(fileitem=file,
                                         meta=meta, mediainfo=mediainfo,
                                         parent=fileitem if file.type == "file" else None,
                                         init_folder=True if file.type == "dir" else False,
                                         overwrite=overwrite)
                # 生成目录的nfo和图片
                if init_folder:
                    # 识别文件夹名称
                    season_meta = MetaInfo(filepath.name)
                    # 当前文件夹为Specials或者SPs时，设置为S0
                    if filepath.name in settings.RENAME_FORMAT_S0_NAMES:
                        season_meta.begin_season = 0
                    if season_meta.begin_season is not None:
                        # 是否已存在
                        nfo_path = filepath / "season.nfo"
                        if overwrite or not self.storagechain.get_file_item(storage=fileitem.storage, path=nfo_path):
                            # 当前目录有季号，生成季nfo
                            season_nfo = self.metadata_nfo(meta=meta, mediainfo=mediainfo,
                                                           season=season_meta.begin_season)
                            if season_nfo:
                                # 写入nfo到根目录
                                __save_file(_fileitem=fileitem, _path=nfo_path, _content=season_nfo)
                            else:
                                logger.warn(f"无法生成电视剧季nfo文件：{meta.name}")
                        else:
                            logger.info(f"已存在nfo文件：{nfo_path}")
                        # TMDB季poster图片
                        image_dict = self.metadata_img(mediainfo=mediainfo, season=season_meta.begin_season)
                        if image_dict:
                            for image_name, image_url in image_dict.items():
                                image_path = filepath.with_name(image_name)
                                if overwrite or not self.storagechain.get_file_item(storage=fileitem.storage,
                                                                                    path=image_path):
                                    # 下载图片
                                    content = __download_image(image_url)
                                    # 保存图片文件到剧集目录
                                    if content:
                                        if not parent:
                                            parent = self.storagechain.get_parent_item(fileitem)
                                        __save_file(_fileitem=parent, _path=image_path, _content=content)
                                else:
                                    logger.info(f"已存在图片文件：{image_path}")
                        # 额外fanart季图片：poster thumb banner
                        image_dict = self.metadata_img(mediainfo=mediainfo)
                        if image_dict:
                            for image_name, image_url in image_dict.items():
                                if image_name.startswith("season"):
                                    image_path = filepath.with_name(image_name)
                                    # 只下载当前刮削季的图片
                                    image_season = "00" if "specials" in image_name else image_name[6:8]
                                    if image_season != str(season_meta.begin_season).rjust(2, '0'):
                                        logger.info(f"当前刮削季为：{season_meta.begin_season}，跳过文件：{image_path}")
                                        continue
                                    if overwrite or not self.storagechain.get_file_item(storage=fileitem.storage,
                                                                                        path=image_path):
                                        # 下载图片
                                        content = __download_image(image_url)
                                        # 保存图片文件到当前目录
                                        if content:
                                            if not parent:
                                                parent = self.storagechain.get_parent_item(fileitem)
                                            __save_file(_fileitem=parent, _path=image_path, _content=content)
                                    else:
                                        logger.info(f"已存在图片文件：{image_path}")
                    # 判断当前目录是不是剧集根目录
                    if not season_meta.season:
                        # 是否已存在
                        nfo_path = filepath / "tvshow.nfo"
                        if overwrite or not self.storagechain.get_file_item(storage=fileitem.storage, path=nfo_path):
                            # 当前目录有名称，生成tvshow nfo 和 tv图片
                            tv_nfo = self.metadata_nfo(meta=meta, mediainfo=mediainfo)
                            if tv_nfo:
                                # 写入tvshow nfo到根目录
                                __save_file(_fileitem=fileitem, _path=nfo_path, _content=tv_nfo)
                            else:
                                logger.warn(f"无法生成电视剧nfo文件：{meta.name}")
                        else:
                            logger.info(f"已存在nfo文件：{nfo_path}")
                        # 生成目录图片
                        image_dict = self.metadata_img(mediainfo=mediainfo)
                        if image_dict:
                            for image_name, image_url in image_dict.items():
                                # 不下载季图片
                                if image_name.startswith("season"):
                                    continue
                                image_path = filepath / image_name
                                if overwrite or not self.storagechain.get_file_item(storage=fileitem.storage,
                                                                                    path=image_path):
                                    # 下载图片
                                    content = __download_image(image_url)
                                    # 保存图片文件到当前目录
                                    if content:
                                        __save_file(_fileitem=fileitem, _path=image_path, _content=content)
                                else:
                                    logger.info(f"已存在图片文件：{image_path}")
        logger.info(f"{filepath.name} 刮削完成")
