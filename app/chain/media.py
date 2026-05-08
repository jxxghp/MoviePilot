import os
from pathlib import Path
from tempfile import NamedTemporaryFile
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
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas import FileItem
from app.schemas.types import (
    ChainEventType,
    EventType,
    MediaType,
    ScrapingTarget,
    ScrapingMetadata,
    ScrapingPolicy,
    SystemConfigKey,
)
from app.utils.http import RequestUtils
from app.utils.mixins import ConfigReloadMixin
from app.utils.singleton import Singleton
from app.utils.string import StringUtils

recognize_lock = Lock()
scraping_lock = Lock()

current_umask = os.umask(0)
os.umask(current_umask)


class ScrapingOption:
    """刮削选项"""

    type: ScrapingTarget = ScrapingTarget.TV
    metadata: ScrapingMetadata = ScrapingMetadata.NFO
    policy: ScrapingPolicy = ScrapingPolicy.MISSINGONLY

    def __init__(
            self,
            type: Union[str, ScrapingTarget],
            metadata: Union[str, ScrapingMetadata],
            value: Union[ScrapingPolicy, bool, str],
    ):
        if isinstance(type, ScrapingTarget):
            self.type = type
        elif isinstance(type, str):
            self.type = ScrapingTarget(type)
        if isinstance(metadata, ScrapingMetadata):
            self.metadata = metadata
        elif isinstance(metadata, str):
            self.metadata = ScrapingMetadata(metadata)
        if isinstance(value, bool):
            # 兼容旧的布尔值格式
            self.policy = ScrapingPolicy.MISSINGONLY if value else ScrapingPolicy.SKIP
        elif isinstance(value, ScrapingPolicy):
            self.policy = value
        elif isinstance(value, str):
            self.policy = ScrapingPolicy(value)
        else:
            logger.error(
                f"无效的刮削选项：type={type}, metadata={metadata}, value={value}"
            )

    @property
    def is_skip(self) -> bool:
        """是否跳过"""
        return self.policy == ScrapingPolicy.SKIP

    @property
    def is_overwrite(self) -> bool:
        """是否覆盖模式"""
        return self.policy == ScrapingPolicy.OVERWRITE


class ScrapingConfig:
    """媒体刮削配置"""

    _policies: dict[tuple[str], ScrapingOption] = {}

    def __init__(self, config_dict: dict[str, str] = None):
        """
        初始化配置对象
        :param config_dict: 用户配置字典（扁平化格式），为 None 时使用默认配置
        """
        # 合并用户配置和默认配置
        if config_dict is None:
            config_dict = {}

        # 以默认配置为基础，用用户配置覆盖
        _config = self.get_default_config()
        for key, value in config_dict.items():
            _config[key] = value

        for key, value in _config.items():
            if "_" in key:
                items = key.split("_", 1)
                self._policies[tuple(items)] = ScrapingOption(*items, value)

    def option(
            self, item: Union[str, ScrapingTarget], metadata: Union[str, ScrapingMetadata]
    ) -> ScrapingOption:

        if isinstance(item, ScrapingTarget):
            item = item.name.lower()
        if isinstance(metadata, ScrapingMetadata):
            metadata = metadata.name.lower()

        return self._policies.get(
            (item, metadata), ScrapingOption(item, metadata, ScrapingPolicy.SKIP)
        )

    @classmethod
    def from_system_config(cls) -> "ScrapingConfig":
        """
        从系统配置加载

        :return: MediaScrapingConfig 实例
        """
        user_config = SystemConfigOper().get(SystemConfigKey.ScrapingSwitchs) or {}
        return cls(user_config)

    @staticmethod
    def get_default_config() -> dict[str, str]:
        """获取默认配置字典"""
        config_items = [
            f"{mt}_{md}"
            for mt, mds in [
                (
                    "movie",
                    ["nfo", "poster", "backdrop", "logo", "disc", "banner", "thumb"],
                ),
                ("tv", ["nfo", "poster", "backdrop", "logo", "banner", "thumb"]),
                ("season", ["nfo", "poster", "banner", "thumb"]),
                ("episode", ["nfo", "thumb"]),
            ]
            for md in mds
        ]
        return {item: ScrapingPolicy.MISSINGONLY for item in config_items}


class MediaChain(ChainBase, ConfigReloadMixin, metaclass=Singleton):
    """
    媒体信息处理链，单例运行
    """

    CONFIG_WATCH = {SystemConfigKey.ScrapingSwitchs.value}

    IMAGE_METADATA_MAP = {
        "poster": ScrapingMetadata.POSTER,
        "backdrop": ScrapingMetadata.BACKDROP,
        "fanart": ScrapingMetadata.BACKDROP,
        "background": ScrapingMetadata.BACKDROP,
        "logo": ScrapingMetadata.LOGO,
        "disc": ScrapingMetadata.DISC,
        "cdart": ScrapingMetadata.DISC,
        "banner": ScrapingMetadata.BANNER,
        "thumb": ScrapingMetadata.THUMB,
    }

    def __init__(self):
        super().__init__()
        self.storagechain = StorageChain()
        self.scraping_policies = ScrapingConfig.from_system_config()

    def on_config_changed(self):
        self.scraping_policies = ScrapingConfig.from_system_config()

    @staticmethod
    def _should_scrape(
            scraping_option: ScrapingOption,
            file_exists: bool,
            global_overwrite: bool = False,
    ) -> bool:
        """
        判断是否应该执行刮削操作

        :param scraping_option: 刮削选项对象
        :param file_exists: 文件是否已存在
        :param global_overwrite: 全局覆盖标志
        :return bool: 是否应该刮削
        """
        if scraping_option.is_skip:
            logger.info(
                f"{scraping_option.type.value} {scraping_option.metadata.value} 刮削策略 {scraping_option.policy.value}"
            )
            return False

        if not file_exists:
            # 文件不存在
            return True

        # 文件存在的情况
        if scraping_option.is_overwrite or global_overwrite:
            logger.info(
                f"{scraping_option.type.value} {scraping_option.metadata.value} 文件存在，"
                f"{'配置为覆盖' if scraping_option.is_overwrite else '配置为全局覆盖'}"
            )
            return True
        else:
            logger.info(
                f"{scraping_option.type.value} {scraping_option.metadata.value} 文件已存在，跳过"
            )
            return False

    def _save_file(
            self, fileitem: schemas.FileItem, path: Path, content: Union[bytes, str]
    ):
        """
        保存或上传文件

        :param fileitem: 关联的媒体文件项
        :param path: 元数据文件路径
        :param content: 文件内容
        """
        if not fileitem or not content or not path:
            return
        # 使用tempfile创建临时文件
        with NamedTemporaryFile(
                delete=True, delete_on_close=False, suffix=path.suffix
        ) as tmp_file:
            tmp_file_path = Path(tmp_file.name)
            # 写入内容
            if isinstance(content, bytes):
                tmp_file.write(content)
            else:
                tmp_file.write(content.encode("utf-8"))
            tmp_file.flush()
            tmp_file.close()  # 关闭文件句柄

            # 刮削文件只需要读写权限
            tmp_file_path.chmod(0o666 & ~current_umask)

            # 上传文件
            item = self.storagechain.upload_file(
                fileitem=fileitem, path=tmp_file_path, new_name=path.name
            )
            if item:
                logger.info(f"已保存文件：{item.path}")
            else:
                logger.warn(f"文件保存失败：{path}")

    def _download_and_save_image(
            self, fileitem: schemas.FileItem, path: Path, url: str
    ):
        """
        流式下载图片并保存到文件

        :param fileitem: 关联的媒体文件项
        :param path: 图片文件路径
        :param url: 图片下载URL
        """
        if not fileitem or not url or not path:
            return
        try:
            logger.info(f"正在下载图片：{url} ...")
            request_utils = RequestUtils(
                proxies=settings.PROXY, ua=settings.NORMAL_USER_AGENT
            )
            with request_utils.get_stream(url=url) as r:
                if r and r.status_code == 200:
                    # 使用tempfile创建临时文件，自动删除
                    with NamedTemporaryFile(
                            delete=True, delete_on_close=False, suffix=path.suffix
                    ) as tmp_file:
                        tmp_file_path = Path(tmp_file.name)
                        # 流式写入文件
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                tmp_file.write(chunk)
                        tmp_file.flush()
                        tmp_file.close()  # 关闭文件句柄

                        # 刮削的图片只需要读写权限
                        tmp_file_path.chmod(0o666 & ~current_umask)

                        # 上传文件
                        item = self.storagechain.upload_file(
                            fileitem=fileitem, path=tmp_file_path, new_name=path.name
                        )
                        if item:
                            logger.info(f"已保存图片：{item.path}")
                        else:
                            logger.warn(f"图片保存失败：{path}")
                else:
                    logger.info(f"{url} 图片下载失败")
        except Exception as err:
            logger.error(f"{url} 图片下载失败：{str(err)}！")

    def _get_target_fileitem_and_path(
            self,
            current_fileitem: schemas.FileItem,
            item_type: ScrapingTarget,
            metadata_type: ScrapingMetadata,
            filename_hint: Optional[str] = None,
            parent_fileitem: Optional[schemas.FileItem] = None,
    ) -> Tuple[schemas.FileItem, Optional[Path]]:
        """
        根据当前上下文、刮削项类型和元数据类型生成目标 FileItem 和 Path
        处理 NFO 和图片文件的命名约定及存储位置
        """
        # 默认保存的目录是当前文件项的目录
        target_dir_item = current_fileitem
        target_dir_path = Path(current_fileitem.path)
        final_filename = filename_hint  # 如果提供了 filename_hint，优先使用

        # 针对 NFO 文件的特殊命名和存储逻辑
        if metadata_type == ScrapingMetadata.NFO:
            if item_type == ScrapingTarget.MOVIE:
                if current_fileitem.type == "file":
                    # 电影文件NFO: 放在电影文件同级目录，名称与电影文件主体一致，后缀.nfo
                    final_filename = f"{target_dir_path.stem}.nfo"
                    target_dir_item = (
                            parent_fileitem
                            or self.storagechain.get_parent_item(current_fileitem)
                    )
                    if not target_dir_item:
                        logger.error(
                            f"无法获取文件 {current_fileitem.path} 的父目录项。"
                        )
                        return (
                            current_fileitem,
                            None,
                        )  # 返回一个表示失败的FileItem和None
                    target_dir_path = Path(target_dir_item.path)
                else:  # current_fileitem.type == "dir"
                    # 电影目录NFO (例如蓝光原盘): 放在电影目录内，名称与目录名主体一致，后缀.nfo
                    final_filename = f"{target_dir_path.name}.nfo"
                    # target_dir_item 保持为 current_fileitem
                    # target_dir_path 保持为 Path(current_fileitem.path)
            elif item_type == ScrapingTarget.TV:
                # 电视剧根目录NFO: 放在剧集根目录内，命名为 tvshow.nfo
                final_filename = "tvshow.nfo"
            elif item_type == ScrapingTarget.SEASON:
                # 电视剧季目录NFO: 放在季目录内，命名为 season.nfo
                final_filename = "season.nfo"
            elif item_type == ScrapingTarget.EPISODE:
                # 电视剧集文件NFO: 放在集文件同级目录，名称与集文件主体一致，后缀.nfo
                final_filename = f"{target_dir_path.stem}.nfo"
                target_dir_item = parent_fileitem or self.storagechain.get_parent_item(
                    current_fileitem
                )
                if not target_dir_item:
                    logger.error(f"无法获取文件 {current_fileitem.path} 的父目录项。")
                    return current_fileitem, None  # 返回一个表示失败的FileItem和None
                target_dir_path = Path(target_dir_item.path)
        # 图片通常是放在当前目录 (current_fileitem) 下
        # Jellyfin/Kodi 等在季目录内使用通用图片名，而不是 season01-poster.jpg
        elif item_type == ScrapingTarget.SEASON:
            season_image_name_map = {
                ScrapingMetadata.POSTER: "poster",
                ScrapingMetadata.BANNER: "banner",
                ScrapingMetadata.THUMB: "thumb",
            }
            if season_image_name := season_image_name_map.get(metadata_type):
                hint_ext = Path(filename_hint).suffix if filename_hint else ".jpg"
                final_filename = f"{season_image_name}{hint_ext}"
        # 如果是 EPISODE 类型的图片（如thumb），通常也是放在文件同级目录，文件名与视频文件一致
        elif (
                metadata_type in [ScrapingMetadata.THUMB]
                and item_type == ScrapingTarget.EPISODE
        ):
            hint_ext = Path(filename_hint).suffix if filename_hint else ".jpg"
            final_filename = f"{target_dir_path.stem}{hint_ext}"
            target_dir_item = parent_fileitem or self.storagechain.get_parent_item(
                current_fileitem
            )
            if not target_dir_item:
                logger.error(f"无法获取文件 {current_fileitem.path} 的父目录项。")
                return current_fileitem, None  # 返回一个表示失败的FileItem和None
            target_dir_path = Path(target_dir_item.path)
        # TODO: 考虑其他图片类型是否也需要保存到父目录

        # 确保最终有文件名
        if not final_filename:
            logger.error(
                f"无法为 {item_type.value} - {metadata_type.value} 确定文件名。filename_hint: {filename_hint}"
            )
            # 返回一个表示失败的FileItem和None
            return current_fileitem, None

        target_full_path = target_dir_path / final_filename
        return target_dir_item, target_full_path

    def metadata_nfo(
            self,
            meta: MetaBase,
            mediainfo: MediaInfo,
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> Optional[str]:
        """
        获取NFO文件内容文本

        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        return self.run_module(
            "metadata_nfo",
            meta=meta,
            mediainfo=mediainfo,
            season=season,
            episode=episode,
        )

    @staticmethod
    def select_recognize_source(
            log_name: str, log_context: str, native_fn, plugin_fn
    ) -> Optional[MediaInfo]:
        """
        选择识别模式，插件优先或原生优先

        :param log_name: 用于日志“标题：...”处的名称（如 file_path.name 或 title）
        :param log_context: 用于日志“未识别到...的媒体信息”处的上下文（如 path 或 title）
        :param native_fn: 原生识别函数
        :param plugin_fn: 插件识别函数
        """
        mediainfo = None
        plugin_available = eventmanager.check(ChainEventType.NameRecognize)
        if settings.RECOGNIZE_PLUGIN_FIRST and plugin_available:
            # 插件优先
            logger.info(f"插件优先模式已开启。请求辅助识别，标题：{log_name} ...")
            mediainfo = plugin_fn()
            if not mediainfo:
                logger.info(
                    f"辅助识别未识别到 {log_context} 的媒体信息，尝试使用原生识别"
                )
                mediainfo = native_fn()
        else:
            # 原生优先
            logger.info(f"插件优先模式未开启。尝试原生识别，标题：{log_name} ...")
            mediainfo = native_fn()
            if not mediainfo and plugin_available:
                logger.info(
                    f"原生识别未识别到 {log_context} 的媒体信息，尝试使用辅助识别"
                )
                mediainfo = plugin_fn()
        return mediainfo

    def recognize_by_meta(
            self, metainfo: MetaBase, episode_group: Optional[str] = None
    ) -> Optional[MediaInfo]:
        """
        根据主副标题识别媒体信息
        """
        title = metainfo.title
        # 按 config 中设置的识别顺序识别
        mediainfo = self.select_recognize_source(
            log_name=title,
            log_context=title,
            native_fn=lambda: self.recognize_media(
                meta=metainfo, episode_group=episode_group
            ),
            plugin_fn=lambda: self.recognize_help(title=title, org_meta=metainfo),
        )
        if not mediainfo:
            logger.warn(f"{title} 未识别到媒体信息")
            return None
        # 识别成功
        logger.info(
            f"{title} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}"
        )
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
                "title": title,
            },
        )
        if not result:
            return None
        # 获取返回事件数据
        event_data = result.event_data or {}
        logger.info(f"获取到辅助识别结果：{event_data}")
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
        if title == "Unknown":
            return None
        if not str(year).isdigit():
            year = None
        # 结果赋值
        if title == org_meta.name and year == org_meta.year:
            logger.info(f"辅助识别与原始识别结果一致，无需重新识别媒体信息")
            return None
        logger.info(f"辅助识别结果与原始识别结果不一致，重新匹配媒体信息 ...")
        org_meta.name = title
        org_meta.year = year
        org_meta.begin_season = season_number
        org_meta.begin_episode = episode_number
        if org_meta.begin_season is not None or org_meta.begin_episode is not None:
            org_meta.type = MediaType.TV
        # 重新识别
        return self.recognize_media(meta=org_meta)

    def recognize_by_path(
            self, path: str, episode_group: Optional[str] = None
    ) -> Optional[Context]:
        """
        根据文件路径识别媒体信息
        """
        logger.info(f"开始识别媒体信息，文件：{path} ...")
        file_path = Path(path)
        # 元数据
        file_meta = MetaInfoPath(file_path)
        # 按 config 中设置的识别顺序识别
        mediainfo = self.select_recognize_source(
            log_name=file_path.name,
            log_context=path,
            native_fn=lambda: self.recognize_media(
                meta=file_meta, episode_group=episode_group
            ),
            plugin_fn=lambda: self.recognize_help(title=path, org_meta=file_meta),
        )
        if not mediainfo:
            logger.warn(f"{path} 未识别到媒体信息")
            return Context(meta_info=file_meta)
        logger.info(
            f"{path} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}"
        )
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
        mtype, key_word, season_num, episode_num, year, content = (
            StringUtils.get_keyword(title)
        )
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

    def get_tmdbinfo_by_doubanid(
            self, doubanid: str, mtype: MediaType = None
    ) -> Optional[dict]:
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
            if isinstance(doubaninfo.get("media_type"), MediaType):
                meta.type = doubaninfo.get("media_type")
            else:
                meta.type = (
                    MediaType.MOVIE
                    if doubaninfo.get("type") == "movie"
                    else MediaType.TV
                )
            # 匹配TMDB信息
            meta_names = list(
                dict.fromkeys(
                    [k for k in [meta_org.name, meta.cn_name, meta.en_name] if k]
                )
            )
            tmdbinfo = self._match_tmdb_with_names(
                meta_names=meta_names,
                year=meta.year,
                mtype=mtype or meta.type,
                season=meta.begin_season,
            )
            if tmdbinfo:
                # 合季季后返回
                tmdbinfo["season"] = meta.begin_season
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
            year = self._extract_year_from_bangumi(bangumiinfo)
            # 识别TMDB媒体信息
            meta_names = list(
                dict.fromkeys([k for k in [meta_cn.name, meta.name] if k])
            )
            tmdbinfo = self._match_tmdb_with_names(
                meta_names=meta_names,
                year=year,
                mtype=MediaType.TV,
                season=meta.begin_season,
            )
            return tmdbinfo
        return None

    def get_doubaninfo_by_tmdbid(
            self, tmdbid: int, mtype: MediaType = None, season: Optional[int] = None
    ) -> Optional[dict]:
        """
        根据TMDBID获取豆瓣信息
        """
        tmdbinfo = self.tmdb_info(tmdbid=tmdbid, mtype=mtype)
        if tmdbinfo:
            # 名称
            name = tmdbinfo.get("title") or tmdbinfo.get("name")
            # 年份
            year = self._extract_year_from_tmdb(tmdbinfo, season)
            # IMDBID
            imdbid = tmdbinfo.get("external_ids", {}).get("imdb_id")
            return self.match_doubaninfo(
                name=name, year=year, mtype=mtype, imdbid=imdbid
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
            year = self._extract_year_from_bangumi(bangumiinfo)
            # 使用名称识别豆瓣媒体信息
            return self.match_doubaninfo(
                name=meta.name, year=year, mtype=MediaType.TV, season=meta.begin_season
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
        # 媒体根目录
        fileitem: FileItem = event_data.get("fileitem")
        # 媒体文件列表
        file_list: List[str] = event_data.get("file_list", [])
        # 媒体元数据
        meta: MetaBase = event_data.get("meta")
        # 媒体信息
        mediainfo: MediaInfo = event_data.get("mediainfo")
        # 是否覆盖
        overwrite = event_data.get("overwrite", False)
        # 检查媒体根目录
        if not fileitem:
            return

        # 刮削锁
        with scraping_lock:
            # 检查文件项是否存在
            if not self.storagechain.get_item(fileitem):
                logger.warn(f"文件项不存在：{fileitem.path}")
                return
            # 检查是否为目录
            if fileitem.type == "file":
                # 单个文件刮削
                self.scrape_metadata(
                    fileitem=fileitem,
                    mediainfo=mediainfo,
                    init_folder=False,
                    parent=self.storagechain.get_parent_item(fileitem),
                    overwrite=overwrite,
                )
            else:
                if file_list:
                    # 如果是BDMV原盘目录，只对根目录进行刮削，不处理子目录
                    if self.storagechain.is_bluray_folder(fileitem):
                        logger.info(
                            f"检测到BDMV原盘目录，只对根目录进行刮削：{fileitem.path}"
                        )
                        self.scrape_metadata(
                            fileitem=fileitem,
                            mediainfo=mediainfo,
                            init_folder=True,
                            recursive=False,
                            overwrite=overwrite,
                        )
                    else:
                        # 1. 收集fileitem和file_list中每个文件之间所有子目录
                        all_dirs = set()
                        root_path = Path(fileitem.path)

                        logger.debug(f"开始收集目录，根目录：{root_path}")
                        # 收集根目录
                        all_dirs.add(root_path)

                        # 收集所有目录（包括所有层级）
                        for sub_file in file_list:
                            sub_path = Path(sub_file)
                            # 收集从根目录到文件的所有父目录
                            current_path = sub_path.parent
                            while (
                                    current_path != root_path
                                    and current_path.is_relative_to(root_path)
                            ):
                                all_dirs.add(current_path)
                                current_path = current_path.parent

                        logger.debug(f"共收集到 {len(all_dirs)} 个目录")

                        # 2. 初始化一遍子目录，但不处理文件
                        for sub_dir in all_dirs:
                            sub_dir_item = self.storagechain.get_file_item(
                                storage=fileitem.storage, path=sub_dir
                            )
                            if sub_dir_item:
                                logger.info(f"为目录生成海报和nfo：{sub_dir}")
                                # 初始化目录元数据，但不处理文件
                                self.scrape_metadata(
                                    fileitem=sub_dir_item,
                                    mediainfo=mediainfo,
                                    init_folder=True,
                                    recursive=False,
                                    overwrite=overwrite,
                                )
                            else:
                                logger.warn(f"无法获取目录项：{sub_dir}")

                        # 3. 刮削每个文件
                        logger.info(f"开始刮削 {len(file_list)} 个文件")
                        for sub_file_path in file_list:
                            sub_file_item = self.storagechain.get_file_item(
                                storage=fileitem.storage, path=Path(sub_file_path)
                            )
                            if sub_file_item:
                                self.scrape_metadata(
                                    fileitem=sub_file_item,
                                    mediainfo=mediainfo,
                                    init_folder=False,
                                    overwrite=overwrite,
                                )
                            else:
                                logger.warn(f"无法获取文件项：{sub_file_path}")
                else:
                    # 执行全量刮削
                    logger.info(f"开始刮削目录 {fileitem.path} ...")
                    self.scrape_metadata(
                        fileitem=fileitem,
                        meta=meta,
                        init_folder=True,
                        mediainfo=mediainfo,
                        overwrite=overwrite,
                    )

    def _scrape_nfo_generic(
            self,
            current_fileitem: schemas.FileItem,
            meta: MetaBase,
            mediainfo: MediaInfo,
            item_type: ScrapingTarget,
            parent_fileitem: Optional[schemas.FileItem] = None,
            overwrite: bool = False,
            season_number: Optional[int] = None,
            episode_number: Optional[int] = None,
    ):
        """
        NFO 刮削
        """
        # 获取刮削选项
        nfo_option = self.scraping_policies.option(item_type, ScrapingMetadata.NFO)

        # 检查刮削开关
        if nfo_option.is_skip:
            logger.info(
                f"{item_type.value} {ScrapingMetadata.NFO.value} 刮削策略 {nfo_option.policy.value}"
            )
            return

        # 获取目标 FileItem (`base_item`) 和 Path (`nfo_path`)
        base_item, nfo_path = self._get_target_fileitem_and_path(
            current_fileitem=current_fileitem,
            item_type=item_type,
            metadata_type=ScrapingMetadata.NFO,
            parent_fileitem=parent_fileitem,
        )

        if not nfo_path:  # _get_target_fileitem_and_path 内部错误处理返回None
            return

        # 文件存在检查
        file_exists = self.storagechain.get_file_item(
            storage=base_item.storage, path=nfo_path
        )

        # 刮削决策
        if self._should_scrape(nfo_option, bool(file_exists), overwrite):
            # 生成 NFO 内容
            nfo_content = self.metadata_nfo(
                meta=meta,
                mediainfo=mediainfo,
                season=season_number,
                episode=episode_number,
            )
            if nfo_content:
                self._save_file(fileitem=base_item, path=nfo_path, content=nfo_content)
            else:
                logger.warn(f"{nfo_path.name} NFO 文件生成失败！")

    def _scrape_images_generic(
            self,
            current_fileitem: schemas.FileItem,
            mediainfo: MediaInfo,
            item_type: ScrapingTarget,
            parent_fileitem: Optional[schemas.FileItem] = None,
            overwrite: bool = False,
            season_number: Optional[int] = None,
            episode_number: Optional[int] = None,
    ):
        """
        图片刮削
        """
        # 获取图片 URL
        if item_type == ScrapingTarget.EPISODE:
            image_dict = self.metadata_img(
                mediainfo=mediainfo, season=season_number, episode=episode_number
            )
        elif item_type == ScrapingTarget.SEASON:
            image_dict = self.metadata_img(mediainfo=mediainfo, season=season_number)
        else:
            image_dict = self.metadata_img(mediainfo=mediainfo)

        if not image_dict:
            logger.info(f"未获取到 {item_type.value} 的图片信息，跳过图片刮削。")
            return

        # 遍历图片 image_name 和 image_url
        for image_name, image_url in image_dict.items():
            metadata_type = None
            # 对每个 image_name 查找匹配的 ScrapingMetadata
            for keyword, meta_type in self.IMAGE_METADATA_MAP.items():
                if keyword in image_name.lower():
                    metadata_type = meta_type
                    break

            if metadata_type:
                # 获取对应的 ScrapingOption
                option = self.scraping_policies.option(item_type, metadata_type)

                if option.is_skip:
                    logger.info(
                        f"{item_type.value} {option.metadata.value} 刮削策略 {option.policy.value}"
                    )
                    continue

                # 判断是否匹配当前刮削的季号
                if item_type == ScrapingTarget.TV and image_name.lower().startswith(
                        "season"
                ):
                    logger.info(f"当前为电视剧根目录刮削，跳过季图片：{image_name}")
                    continue
                if (
                        item_type == ScrapingTarget.SEASON
                        and season_number is not None
                        and image_name.lower().startswith("season")
                ):
                    # 检查是否只下载当前刮削季的图片
                    image_season_str = (
                        "00" if "specials" in image_name.lower() else image_name[6:8]
                    )

                    if image_season_str is not None and image_season_str != str(
                            season_number
                    ).rjust(2, "0"):
                        logger.info(
                            f"当前刮削季为：{season_number}，跳过非本季图片：{image_name}"
                        )
                        continue

                # 获取目标 FileItem (`base_item`) 和 Path (`image_path`)
                base_item, image_path = self._get_target_fileitem_and_path(
                    current_fileitem=current_fileitem,
                    item_type=item_type,
                    metadata_type=metadata_type,
                    filename_hint=image_name,
                    parent_fileitem=parent_fileitem,
                )

                if not image_path:
                    continue

                # 文件存在检查
                file_exists = self.storagechain.get_file_item(
                    storage=base_item.storage, path=image_path
                )

                # 刮削决策
                if self._should_scrape(option, bool(file_exists), overwrite):
                    self._download_and_save_image(
                        fileitem=base_item, path=image_path, url=image_url
                    )
            else:
                logger.debug(
                    f"未找到图片类型 {image_name} 对应的 ScrapingMetadata，跳过。"
                )

    def scrape_metadata(
            self,
            fileitem: schemas.FileItem,
            meta: MetaBase = None,
            mediainfo: MediaInfo = None,
            init_folder: bool = True,
            parent: schemas.FileItem = None,
            overwrite: bool = False,
            recursive: bool = True,
    ):
        """
        手动刮削媒体信息

        :param fileitem: 刮削目录或文件
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param init_folder: 是否刮削根目录
        :param parent: 上级目录
        :param overwrite: 是否覆盖已有文件
        :param recursive: 是否递归处理目录内文件
        """
        if not fileitem:
            return

        # 当前文件路径
        filepath = Path(fileitem.path)
        if fileitem.type == "file" and (
                not filepath.suffix or filepath.suffix.lower() not in settings.RMT_MEDIAEXT
        ):
            return

        # 准备元数据和媒体信息
        if not meta:
            meta = MetaInfoPath(filepath)
        if not mediainfo:
            mediainfo = self.recognize_by_meta(meta)
        if not mediainfo:
            logger.warn(f"{filepath} 无法识别文件媒体信息！")
            return

        logger.info(f"开始刮削：{filepath} ...")

        # 根据媒体类型分发处理逻辑
        if mediainfo.type == MediaType.MOVIE:
            self._handle_movie_scraping(
                fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                init_folder=init_folder,
                parent=parent,
                overwrite=overwrite,
                recursive=recursive,
            )
        else:
            self._handle_tv_scraping(
                fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                init_folder=init_folder,
                parent=parent,
                overwrite=overwrite,
                recursive=recursive,
            )

        logger.info(f"{filepath.name} 刮削完成")

    def _handle_movie_scraping(
            self,
            fileitem: schemas.FileItem,
            meta: MetaBase,
            mediainfo: MediaInfo,
            init_folder: bool,
            parent: schemas.FileItem,
            overwrite: bool,
            recursive: bool,
    ):
        """
        处理电影刮削
        """
        if fileitem.type == "file":
            # 电影文件：仅处理 NFO
            self._scrape_nfo_generic(
                current_fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.MOVIE,
                parent_fileitem=parent,
                overwrite=overwrite,
            )
        else:
            # 电影目录：递归处理文件并初始化目录
            self._handle_movie_directory(
                fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                init_folder=init_folder,
                overwrite=overwrite,
                recursive=recursive,
            )

    def _handle_movie_directory(
            self,
            fileitem: schemas.FileItem,
            meta: MetaBase,
            mediainfo: MediaInfo,
            init_folder: bool,
            overwrite: bool,
            recursive: bool,
    ):
        """
        处理电影目录刮削
        """
        files = self.storagechain.list_files(fileitem=fileitem) or []
        is_bluray_folder = self.storagechain.contains_bluray_subdirectories(files)

        # 递归处理文件（非蓝光原盘）
        if recursive and not is_bluray_folder:
            for file in files:
                if file.type == "dir":
                    continue
                self.scrape_metadata(
                    fileitem=file,
                    mediainfo=mediainfo,
                    init_folder=False,
                    parent=fileitem,
                    overwrite=overwrite,
                )

        # 初始化目录元数据
        if init_folder:
            if is_bluray_folder:
                # 蓝光原盘目录：仅处理 NFO
                self._scrape_nfo_generic(
                    current_fileitem=fileitem,
                    meta=meta,
                    mediainfo=mediainfo,
                    item_type=ScrapingTarget.MOVIE,
                    overwrite=overwrite,
                )
            # 电影目录：处理图片
            self._scrape_images_generic(
                current_fileitem=fileitem,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.MOVIE,
                overwrite=overwrite,
            )

    def _handle_tv_scraping(
            self,
            fileitem: schemas.FileItem,
            meta: MetaBase,
            mediainfo: MediaInfo,
            init_folder: bool,
            parent: schemas.FileItem,
            overwrite: bool,
            recursive: bool,
    ):
        """
        处理电视剧刮削
        """
        filepath = Path(fileitem.path)

        if fileitem.type == "file":
            # 电视剧集文件：重新识别季集信息并刮削
            self._handle_tv_episode_file(
                fileitem=fileitem,
                filepath=filepath,
                mediainfo=mediainfo,
                parent=parent,
                overwrite=overwrite,
            )
        else:
            # 电视剧目录：递归处理并初始化目录
            self._handle_tv_directory(
                fileitem=fileitem,
                filepath=filepath,
                meta=meta,
                mediainfo=mediainfo,
                init_folder=init_folder,
                parent=parent,
                overwrite=overwrite,
                recursive=recursive,
            )

    def _handle_tv_episode_file(
            self,
            fileitem: schemas.FileItem,
            filepath: Path,
            mediainfo: MediaInfo,
            parent: schemas.FileItem,
            overwrite: bool,
    ):
        """
        处理电视剧集文件刮削
        """
        # 重新识别季集信息
        file_meta = MetaInfoPath(filepath)
        if not file_meta.begin_episode:
            logger.warn(f"{filepath.name} 无法识别文件集数！")
            return

        file_mediainfo = self.recognize_media(
            meta=file_meta,
            tmdbid=mediainfo.tmdb_id,
            episode_group=mediainfo.episode_group,
        )
        if not file_mediainfo:
            logger.warn(f"{filepath.name} 无法识别文件媒体信息！")
            return

        # 处理 NFO
        self._scrape_nfo_generic(
            current_fileitem=fileitem,
            meta=file_meta,
            mediainfo=file_mediainfo,
            item_type=ScrapingTarget.EPISODE,
            parent_fileitem=parent,
            overwrite=overwrite,
            season_number=file_meta.begin_season,
            episode_number=file_meta.begin_episode,
        )

        # 处理图片
        self._scrape_images_generic(
            current_fileitem=fileitem,
            mediainfo=file_mediainfo,
            item_type=ScrapingTarget.EPISODE,
            parent_fileitem=parent,
            overwrite=overwrite,
            season_number=file_meta.begin_season,
            episode_number=file_meta.begin_episode,
        )

    def _handle_tv_directory(
            self,
            fileitem: schemas.FileItem,
            filepath: Path,
            meta: MetaBase,
            mediainfo: MediaInfo,
            init_folder: bool,
            parent: schemas.FileItem,
            overwrite: bool,
            recursive: bool,
    ):
        """
        处理电视剧目录刮削
        """
        # 递归处理子目录和文件
        if recursive:
            files = self.storagechain.list_files(fileitem=fileitem) or []
            for file in files:
                if (
                        file.type == "dir"
                        and file.name not in settings.RENAME_FORMAT_S0_NAMES
                        and MetaInfo(file.name).begin_season is None
                ):
                    # 电视剧不处理非季子目录
                    continue
                self.scrape_metadata(
                    fileitem=file,
                    mediainfo=mediainfo,
                    parent=fileitem if file.type == "file" else None,
                    init_folder=True if file.type == "dir" else False,
                    overwrite=overwrite,
                )

        # 初始化目录元数据
        if init_folder:
            self._initialize_tv_directory_metadata(
                fileitem=fileitem,
                filepath=filepath,
                meta=meta,
                mediainfo=mediainfo,
                parent=parent,
                overwrite=overwrite,
            )

    def _initialize_tv_directory_metadata(
            self,
            fileitem: schemas.FileItem,
            filepath: Path,
            meta: MetaBase,
            mediainfo: MediaInfo,
            parent: schemas.FileItem,
            overwrite: bool,
    ):
        """
        初始化电视剧目录元数据（识别季号并刮削）
        """
        # 识别文件夹名称
        season_meta = MetaInfo(filepath.name)

        # 特殊季目录处理（Specials/SPs）
        if filepath.name in settings.RENAME_FORMAT_S0_NAMES:
            season_meta.begin_season = 0
        elif season_meta.name and season_meta.begin_season is not None:
            # 排除辅助词重新识别，避免误判根目录 (issue https://github.com/jxxghp/MoviePilot/issues/5501)
            season_meta_no_custom = MetaInfo(filepath.name, custom_words=["#"])
            if season_meta_no_custom.begin_season is None:
                # 季号由辅助词指定，按剧集根目录处理 (issue https://github.com/jxxghp/MoviePilot/issues/5373)
                season_meta.begin_season = None

        # 根据季号判断目录类型并刮削
        if season_meta.begin_season is not None:
            # 季目录：处理季 NFO 和图片
            self._scrape_nfo_generic(
                current_fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.SEASON,
                overwrite=overwrite,
                season_number=season_meta.begin_season,
            )
            self._scrape_images_generic(
                current_fileitem=fileitem,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.SEASON,
                parent_fileitem=parent,
                overwrite=overwrite,
                season_number=season_meta.begin_season,
            )
        elif season_meta.name:
            # 剧集根目录：处理电视剧 NFO 和图片
            self._scrape_nfo_generic(
                current_fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.TV,
                overwrite=overwrite,
            )
            self._scrape_images_generic(
                current_fileitem=fileitem,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.TV,
                overwrite=overwrite,
            )
        else:
            logger.warn("无法识别元数据，跳过")

    @staticmethod
    async def async_select_recognize_source(
            log_name: str, log_context: str, native_fn, plugin_fn
    ) -> Optional[MediaInfo]:
        """
        选择识别模式，插件优先或原生优先（异步版本）

        :param log_name: 用于日志“标题：...”处的名称（如 file_path.name 或 title）
        :param log_context: 用于日志“未识别到...的媒体信息”处的上下文（如 path 或 title）
        :param native_fn: 原生识别函数
        :param plugin_fn: 插件识别函数
        """
        mediainfo = None
        plugin_available = eventmanager.check(ChainEventType.NameRecognize)
        if settings.RECOGNIZE_PLUGIN_FIRST and plugin_available:
            # 插件优先
            logger.info(f"插件优先模式已开启。请求辅助识别，标题：{log_name} ...")
            mediainfo = await plugin_fn()
            if not mediainfo:
                logger.info(
                    f"辅助识别未识别到 {log_context} 的媒体信息，尝试使用原生识别"
                )
                mediainfo = await native_fn()
        else:
            # 原生优先
            logger.info(f"识别标题：{log_name} ...")
            mediainfo = await native_fn()
            if not mediainfo and plugin_available:
                logger.info(
                    f"原生识别未识别到 {log_context} 的媒体信息，尝试使用辅助识别"
                )
                mediainfo = await plugin_fn()
        return mediainfo

    async def async_recognize_by_meta(
            self, metainfo: MetaBase, episode_group: Optional[str] = None
    ) -> Optional[MediaInfo]:
        """
        根据主副标题识别媒体信息（异步版本）
        """
        title = metainfo.title

        # 定义识别函数
        async def native_recognize():
            return await self.async_recognize_media(
                meta=metainfo, episode_group=episode_group
            )

        async def plugin_recognize():
            return await self.async_recognize_help(title=title, org_meta=metainfo)

        # 按 config 中设置的识别顺序识别
        mediainfo = await self.async_select_recognize_source(
            log_name=title,
            log_context=title,
            native_fn=native_recognize,
            plugin_fn=plugin_recognize,
        )
        if not mediainfo:
            logger.warn(f"{title} 未识别到媒体信息")
            return None
        # 识别成功
        logger.info(
            f"{title} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}"
        )
        # 更新媒体图片
        await self.async_obtain_images(mediainfo=mediainfo)
        # 返回上下文
        return mediainfo

    async def async_recognize_help(
            self, title: str, org_meta: MetaBase
    ) -> Optional[MediaInfo]:
        """
        请求辅助识别，返回媒体信息（异步版本）

        :param title: 标题
        :param org_meta: 原始元数据
        """
        # 发送请求事件，等待结果
        result: Event = await eventmanager.async_send_event(
            ChainEventType.NameRecognize,
            {
                "title": title,
            },
        )
        if not result:
            return None
        # 获取返回事件数据
        event_data = result.event_data or {}
        logger.info(f"获取到辅助识别结果：{event_data}")
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
        if title == "Unknown":
            return None
        if not str(year).isdigit():
            year = None
        # 结果赋值
        if title == org_meta.name and year == org_meta.year:
            logger.info(f"辅助识别与原始识别结果一致，无需重新识别媒体信息")
            return None
        logger.info(f"辅助识别结果与原始识别结果不一致，重新匹配媒体信息 ...")
        org_meta.name = title
        org_meta.year = year
        org_meta.begin_season = season_number
        org_meta.begin_episode = episode_number
        if org_meta.begin_season or org_meta.begin_episode:
            org_meta.type = MediaType.TV
        # 重新识别
        return await self.async_recognize_media(meta=org_meta)

    async def async_recognize_by_path(
            self, path: str, episode_group: Optional[str] = None
    ) -> Optional[Context]:
        """
        根据文件路径识别媒体信息（异步版本）
        """
        logger.info(f"开始识别媒体信息，文件：{path} ...")
        file_path = Path(path)
        # 元数据
        file_meta = MetaInfoPath(file_path)

        # 定义识别函数
        async def native_recognize():
            return await self.async_recognize_media(
                meta=file_meta, episode_group=episode_group
            )

        async def plugin_recognize():
            return await self.async_recognize_help(title=path, org_meta=file_meta)

        # 按 config 中设置的识别顺序识别
        mediainfo = await self.async_select_recognize_source(
            log_name=file_path.name,
            log_context=path,
            native_fn=native_recognize,
            plugin_fn=plugin_recognize,
        )
        if not mediainfo:
            logger.warn(f"{path} 未识别到媒体信息")
            return Context(meta_info=file_meta)
        logger.info(
            f"{path} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}"
        )
        # 更新媒体图片
        await self.async_obtain_images(mediainfo=mediainfo)
        # 返回上下文
        return Context(meta_info=file_meta, media_info=mediainfo)

    async def async_search(
            self, title: str
    ) -> Tuple[Optional[MetaBase], List[MediaInfo]]:
        """
        搜索媒体/人物信息（异步版本）

        :param title: 搜索内容
        :return: 识别元数据，媒体信息列表
        """
        # 提取要素
        mtype, key_word, season_num, episode_num, year, content = (
            StringUtils.get_keyword(title)
        )
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
        medias: Optional[List[MediaInfo]] = await self.async_search_medias(meta=meta)
        if not medias:
            logger.warn(f"{meta.name} 没有找到对应的媒体信息！")
            return meta, []
        logger.info(f"{content} 搜索到 {len(medias)} 条相关媒体信息")
        # 识别的元数据，媒体信息列表
        return meta, medias

    @staticmethod
    def _extract_year_from_bangumi(bangumiinfo: dict) -> Optional[str]:
        """
        从Bangumi信息中提取年份
        """
        release_date = bangumiinfo.get("date") or bangumiinfo.get("air_date")
        if release_date:
            return release_date[:4]
        return None

    @staticmethod
    def _extract_year_from_tmdb(
            tmdbinfo: dict, season: Optional[int] = None
    ) -> Optional[str]:
        """
        从TMDB信息中提取年份
        """
        year = None
        if tmdbinfo.get("release_date"):
            year = tmdbinfo["release_date"][:4]
        elif tmdbinfo.get("seasons") and season is not None:
            for seainfo in tmdbinfo["seasons"]:
                season_number = seainfo.get("season_number")
                if season_number is None:
                    continue
                air_date = seainfo.get("air_date")
                if air_date and season_number == season:
                    year = air_date[:4]
                    break
        return year

    def _match_tmdb_with_names(
            self,
            meta_names: list,
            year: Optional[str],
            mtype: MediaType,
            season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        使用名称列表匹配TMDB信息
        """
        for name in meta_names:
            tmdbinfo = self.match_tmdbinfo(
                name=name, year=year, mtype=mtype, season=season
            )
            if tmdbinfo:
                return tmdbinfo
        return None

    async def _async_match_tmdb_with_names(
            self,
            meta_names: list,
            year: Optional[str],
            mtype: MediaType,
            season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        使用名称列表匹配TMDB信息（异步版本）
        """
        for name in meta_names:
            tmdbinfo = await self.async_match_tmdbinfo(
                name=name, year=year, mtype=mtype, season=season
            )
            if tmdbinfo:
                return tmdbinfo
        return None

    async def async_get_tmdbinfo_by_doubanid(
            self, doubanid: str, mtype: MediaType = None
    ) -> Optional[dict]:
        """
        根据豆瓣ID获取TMDB信息（异步版本）
        """
        tmdbinfo = None
        doubaninfo = await self.async_douban_info(doubanid=doubanid, mtype=mtype)
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
            if isinstance(doubaninfo.get("media_type"), MediaType):
                meta.type = doubaninfo.get("media_type")
            else:
                meta.type = (
                    MediaType.MOVIE
                    if doubaninfo.get("type") == "movie"
                    else MediaType.TV
                )
            # 匹配TMDB信息
            meta_names = list(
                dict.fromkeys(
                    [k for k in [meta_org.name, meta.cn_name, meta.en_name] if k]
                )
            )
            tmdbinfo = await self._async_match_tmdb_with_names(
                meta_names=meta_names,
                year=meta.year,
                mtype=mtype or meta.type,
                season=meta.begin_season,
            )
            if tmdbinfo:
                # 合季季后返回
                tmdbinfo["season"] = meta.begin_season
        return tmdbinfo

    async def async_get_tmdbinfo_by_bangumiid(self, bangumiid: int) -> Optional[dict]:
        """
        根据BangumiID获取TMDB信息（异步版本）
        """
        bangumiinfo = await self.async_bangumi_info(bangumiid=bangumiid)
        if bangumiinfo:
            # 优先使用原标题匹配
            if bangumiinfo.get("name_cn"):
                meta = MetaInfo(title=bangumiinfo.get("name"))
                meta_cn = MetaInfo(title=bangumiinfo.get("name_cn"))
            else:
                meta_cn = meta = MetaInfo(title=bangumiinfo.get("name"))
            # 年份
            year = self._extract_year_from_bangumi(bangumiinfo)
            # 识别TMDB媒体信息
            meta_names = list(
                dict.fromkeys([k for k in [meta_cn.name, meta.name] if k])
            )
            tmdbinfo = await self._async_match_tmdb_with_names(
                meta_names=meta_names,
                year=year,
                mtype=MediaType.TV,
                season=meta.begin_season,
            )
            return tmdbinfo
        return None

    async def async_get_doubaninfo_by_tmdbid(
            self, tmdbid: int, mtype: MediaType = None, season: Optional[int] = None
    ) -> Optional[dict]:
        """
        根据TMDBID获取豆瓣信息（异步版本）
        """
        tmdbinfo = await self.async_tmdb_info(tmdbid=tmdbid, mtype=mtype)
        if tmdbinfo:
            # 名称
            name = tmdbinfo.get("title") or tmdbinfo.get("name")
            # 年份
            year = self._extract_year_from_tmdb(tmdbinfo, season)
            # IMDBID
            imdbid = tmdbinfo.get("external_ids", {}).get("imdb_id")
            return await self.async_match_doubaninfo(
                name=name, year=year, mtype=mtype, imdbid=imdbid
            )
        return None

    async def async_get_doubaninfo_by_bangumiid(self, bangumiid: int) -> Optional[dict]:
        """
        根据BangumiID获取豆瓣信息（异步版本）
        """
        bangumiinfo = await self.async_bangumi_info(bangumiid=bangumiid)
        if bangumiinfo:
            # 优先使用中文标题匹配
            if bangumiinfo.get("name_cn"):
                meta = MetaInfo(title=bangumiinfo.get("name_cn"))
            else:
                meta = MetaInfo(title=bangumiinfo.get("name"))
            # 年份
            year = self._extract_year_from_bangumi(bangumiinfo)
            # 使用名称识别豆瓣媒体信息
            return await self.async_match_doubaninfo(
                name=meta.name, year=year, mtype=MediaType.TV, season=meta.begin_season
            )
        return None
