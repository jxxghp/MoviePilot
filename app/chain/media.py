import copy
import time
from pathlib import Path
from threading import Lock
from typing import Optional, List, Tuple

from app import schemas
from app.chain import ChainBase
from app.core.config import settings
from app.core.context import Context, MediaInfo
from app.core.event import eventmanager, Event
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo, MetaInfoPath
from app.helper.aliyun import AliyunHelper
from app.helper.u115 import U115Helper
from app.log import logger
from app.schemas.types import EventType, MediaType
from app.utils.http import RequestUtils
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

    def meta_nfo(self, meta: MetaBase, mediainfo: MediaInfo,
                 season: int = None, episode: int = None) -> Optional[str]:
        """
        获取NFO文件内容文本
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        return self.run_module("meta_nfo", meta=meta, mediainfo=mediainfo, season=season, episode=episode)

    def recognize_by_meta(self, metainfo: MetaBase) -> Optional[MediaInfo]:
        """
        根据主副标题识别媒体信息
        """
        title = metainfo.title
        # 识别媒体信息
        mediainfo: MediaInfo = self.recognize_media(meta=metainfo)
        if not mediainfo:
            # 尝试使用辅助识别，如果有注册响应事件的话
            if eventmanager.check(EventType.NameRecognize):
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
        for i in range(20):
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
            # 尝试使用辅助识别，如果有注册响应事件的话
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
                                 mtype: MediaType = None, season: int = None) -> Optional[dict]:
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

    def scrape_metadata_online(self, storage: str, fileitem: schemas.FileItem,
                               meta: MetaBase, mediainfo: MediaInfo, init_folder: bool = True):
        """
        远程刮削媒体信息（网盘等）
        """

        def __list_files(_storage: str, _fileid: str, _path: str = None, _drive_id: str = None):
            if _storage == "aliyun":
                return AliyunHelper().list(drive_id=_drive_id, parent_file_id=_fileid, path=_path)
            if _storage == "u115":
                return U115Helper().list(parent_file_id=_fileid, path=_path)
            return []

        def __upload_file(_storage: str, _fileid: str, _path: Path):
            if _storage == "aliyun":
                return AliyunHelper().upload(parent_file_id=_fileid, file_path=_path)
            if _storage == "u115":
                return U115Helper().upload(parent_file_id=_fileid, file_path=_path)

        def __save_image(u: str, f: Path):
            """
            下载图片并保存
            """
            try:
                logger.info(f"正在下载{f.stem}图片：{u} ...")
                r = RequestUtils(proxies=settings.PROXY).get_res(url=u)
                if r:
                    f.write_bytes(r.content)
                else:
                    logger.info(f"{f.stem}图片下载失败，请检查网络连通性！")
            except Exception as err:
                logger.error(f"{f.stem}图片下载失败：{str(err)}！")

        if storage not in ["aliyun", "u115"]:
            logger.warn(f"不支持的存储类型：{storage}")
            return

        # 当前文件路径
        filepath = Path(fileitem.path)
        if fileitem.type == "file" \
                and (not filepath.suffix or filepath.suffix.lower() not in settings.RMT_MEDIAEXT):
            return
        logger.info(f"开始刮削：{filepath} ...")
        if mediainfo.type == MediaType.MOVIE:
            # 电影
            if fileitem.type == "file":
                # 电影文件
                logger.info(f"正在生成电影nfo：{mediainfo.title_year} - {filepath.name}")
                movie_nfo = self.meta_nfo(meta=meta, mediainfo=mediainfo)
                if not movie_nfo:
                    logger.warn(f"{filepath.name} nfo文件生成失败！")
                    return
                # 写入到临时目录
                nfo_path = settings.TEMP_PATH / f"{filepath.stem}.nfo"
                nfo_path.write_bytes(movie_nfo)
                # 上传NFO文件
                logger.info(f"上传NFO文件：{nfo_path.name} ...")
                __upload_file(storage, fileitem.parent_fileid, nfo_path)
                logger.info(f"{nfo_path.name} 上传成功")
            else:
                # 电影目录
                files = __list_files(_storage=storage, _fileid=fileitem.fileid,
                                     _drive_id=fileitem.drive_id, _path=fileitem.path)
                for file in files:
                    self.scrape_metadata_online(storage=storage, fileitem=file,
                                                meta=meta, mediainfo=mediainfo,
                                                init_folder=False)
                # 生成图片文件和上传
                if init_folder:
                    for attr_name, attr_value in vars(mediainfo).items():
                        if attr_value \
                                and attr_name.endswith("_path") \
                                and attr_value \
                                and isinstance(attr_value, str) \
                                and attr_value.startswith("http"):
                            image_name = attr_name.replace("_path", "") + Path(attr_value).suffix
                            # 写入nfo到根目录
                            image_path = settings.TEMP_PATH / image_name
                            __save_image(attr_value, image_path)
                            # 上传图片文件到当前目录
                            logger.info(f"上传图片文件：{image_path.name} ...")
                            __upload_file(storage, fileitem.fileid, image_path)
                            logger.info(f"{image_path.name} 上传成功")
        else:
            # 电视剧
            if fileitem.type == "file":
                # 当前为集文件，重新识别季集
                file_meta = MetaInfoPath(filepath)
                if not file_meta.begin_episode:
                    logger.warn(f"{filepath.name} 无法识别文件集数！")
                    return
                file_mediainfo = self.recognize_media(meta=file_meta)
                if not file_mediainfo:
                    logger.warn(f"{filepath.name} 无法识别文件媒体信息！")
                    return
                # 获取集的nfo文件
                episode_nfo = self.meta_nfo(meta=file_meta, mediainfo=file_mediainfo,
                                            season=file_meta.begin_season, episode=file_meta.begin_episode)
                if not episode_nfo:
                    logger.warn(f"{filepath.name} nfo生成失败！")
                    return
                # 写入到临时目录
                nfo_path = settings.TEMP_PATH / f"{filepath.stem}.nfo"
                nfo_path.write_bytes(episode_nfo)
                # 上传NFO文件，到文件当前目录下
                logger.info(f"上传NFO文件：{nfo_path.name} ...")
                __upload_file(storage, fileitem.parent_fileid, nfo_path)
                logger.info(f"{nfo_path.name} 上传成功")
            elif meta.begin_season:
                # 当前为季的目录，处理目录内的文件
                files = __list_files(_storage=storage, _fileid=fileitem.fileid,
                                     _drive_id=fileitem.drive_id, _path=fileitem.path)
                for file in files:
                    self.scrape_metadata_online(storage=storage, fileitem=file,
                                                meta=meta, mediainfo=mediainfo,
                                                init_folder=False)
                # 生成季的nfo和图片
                if init_folder:
                    # 季nfo
                    season_nfo = self.meta_nfo(meta=meta, mediainfo=mediainfo, season=meta.begin_season)
                    if not season_nfo:
                        logger.warn(f"无法生成电视剧季nfo文件：{meta.name}")
                        return
                    # 写入nfo到根目录
                    nfo_path = settings.TEMP_PATH / "season.nfo"
                    nfo_path.write_bytes(season_nfo)
                    # 上传NFO文件
                    logger.info(f"上传NFO文件：{nfo_path.name} ...")
                    __upload_file(storage, fileitem.fileid, nfo_path)
                    logger.info(f"{nfo_path.name} 上传成功")
                    # TMDB季poster图片
                    sea_seq = str(meta.begin_season).rjust(2, '0')
                    # 查询季剧详情
                    seasoninfo = self.tmdb_info(tmdbid=mediainfo.tmdb_id, mtype=MediaType.TV,
                                                season=meta.begin_season)
                    if not seasoninfo:
                        logger.warn(f"无法获取 {mediainfo.title_year} 第{meta.begin_season}季 的媒体信息！")
                        return
                    if seasoninfo.get("poster_path"):
                        # 下载图片
                        ext = Path(seasoninfo.get('poster_path')).suffix
                        url = f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{seasoninfo.get('poster_path')}"
                        image_path = filepath.parent.with_name(f"season{sea_seq}-poster{ext}")
                        __save_image(url, image_path)
                        # 上传图片文件到当前目录
                        logger.info(f"上传图片文件：{image_path.name} ...")
                        __upload_file(storage, fileitem.fileid, image_path)
                        logger.info(f"{image_path.name} 上传成功")
                    # 季的其它图片
                    for attr_name, attr_value in vars(mediainfo).items():
                        if attr_value \
                                and attr_name.startswith("season") \
                                and not attr_name.endswith("poster_path") \
                                and attr_value \
                                and isinstance(attr_value, str) \
                                and attr_value.startswith("http"):
                            image_name = attr_name.replace("_path", "") + Path(attr_value).suffix
                            image_path = filepath.parent.with_name(image_name)
                            __save_image(attr_value, image_path)
                            # 上传图片文件到当前目录
                            logger.info(f"上传图片文件：{image_path.name} ...")
                            __upload_file(storage, fileitem.fileid, image_path)
                            logger.info(f"{image_path.name} 上传成功")
            else:
                # 当前为根目录，处理目录内的文件
                files = __list_files(_storage=storage, _fileid=fileitem.fileid,
                                     _drive_id=fileitem.drive_id, _path=fileitem.path)
                for file in files:
                    self.scrape_metadata_online(storage=storage, fileitem=file,
                                                meta=meta, mediainfo=mediainfo,
                                                init_folder=False)
                # 生成根目录的nfo和图片
                if init_folder:
                    tv_nfo = self.meta_nfo(meta=meta, mediainfo=mediainfo)
                    if not tv_nfo:
                        logger.warn(f"无法生成电视剧nfo文件：{meta.name}")
                        return
                    # 写入nfo到根目录
                    nfo_path = settings.TEMP_PATH / "tvshow.nfo"
                    nfo_path.write_bytes(tv_nfo)
                    # 上传NFO文件
                    logger.info(f"上传NFO文件：{nfo_path.name} ...")
                    __upload_file(storage, fileitem.fileid, nfo_path)
                    logger.info(f"{nfo_path.name} 上传成功")
                    # 生成根目录图片
                    for attr_name, attr_value in vars(mediainfo).items():
                        if attr_name \
                                and attr_name.endswith("_path") \
                                and not attr_name.startswith("season") \
                                and attr_value \
                                and isinstance(attr_value, str) \
                                and attr_value.startswith("http"):
                            image_name = attr_name.replace("_path", "") + Path(attr_value).suffix
                            image_path = filepath.parent.with_name(image_name)
                            __save_image(attr_value, image_path)
                            # 上传图片文件到当前目录
                            logger.info(f"上传图片文件：{image_path.name} ...")
                            __upload_file(storage, fileitem.fileid, image_path)
                            logger.info(f"{image_path.name} 上传成功")

        logger.info(f"{filepath.name} 刮削完成")
