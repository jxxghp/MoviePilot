from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import List, Tuple, Dict, Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.nfo import NfoReader
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType
from app.utils.system import SystemUtils


class LibraryScraper(_PluginBase):

    # 插件名称
    plugin_name = "媒体库刮削"
    # 插件描述
    plugin_desc = "定时对媒体库进行刮削，补齐缺失元数据和图片。"
    # 插件图标
    plugin_icon = "scraper.png"
    # 主题色
    plugin_color = "#FF7D00"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "libraryscraper_"
    # 加载顺序
    plugin_order = 7
    # 可使用的用户级别
    user_level = 1

    # 私有属性
    transferhis = None
    _scheduler = None
    _scraper = None
    # 限速开关
    _enabled = False
    _onlyonce = False
    _cron = None
    _mode = ""
    _scraper_paths = ""
    _exclude_paths = ""
    # 退出事件
    _event = Event()
    
    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._mode = config.get("mode") or ""
            self._scraper_paths = config.get("scraper_paths") or ""
            self._exclude_paths = config.get("exclude_paths") or ""

        # 停止现有任务
        self.stop_service()

        # 启动定时任务 & 立即运行一次
        if self._enabled or self._onlyonce:
            self.transferhis = TransferHistoryOper()
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._cron:
                logger.info(f"媒体库刮削服务启动，周期：{self._cron}")
                try:
                    self._scheduler.add_job(func=self.__libraryscraper,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="媒体库刮削")
                except Exception as e:
                    logger.error(f"媒体库刮削服务启动失败，原因：{str(e)}")
                    self.systemmessage.put(f"媒体库刮削服务启动失败，原因：{str(e)}")
            else:
                logger.info(f"媒体库刮削服务启动，周期：每7天")
                self._scheduler.add_job(func=self.__libraryscraper,
                                        trigger=CronTrigger.from_crontab("0 0 */7 * *"),
                                        name="媒体库刮削")
            if self._onlyonce:
                logger.info(f"媒体库刮削服务，立即运行一次")
                self._scheduler.add_job(func=self.__libraryscraper, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="Cloudflare优选")
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "onlyonce": False,
                    "enabled": self._enabled,
                    "cron": self._cron,
                    "mode": self._mode,
                    "scraper_paths": self._scraper_paths,
                    "exclude_paths": self._exclude_paths
                })
            if self._scheduler.get_jobs():
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'mode',
                                            'label': '刮削模式',
                                            'items': [
                                                {'title': '仅刮削缺失元数据和图片', 'value': ''},
                                                {'title': '覆盖所有元数据和图片', 'value': 'force_all'},
                                                {'title': '覆盖所有元数据', 'value': 'force_nfo'},
                                                {'title': '覆盖所有图片', 'value': 'force_image'},
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'scraper_paths',
                                            'label': '削刮路径',
                                            'rows': 5,
                                            'placeholder': '每一行一个目录，需配置到媒体文件的上级目录，即开了二级分类时需要配置到二级分类目录'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'exclude_paths',
                                            'label': '排除路径',
                                            'rows': 2,
                                            'placeholder': '每一行一个目录'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "cron": "0 0 */7 * *",
            "mode": "",
            "scraper_paths": "",
            "err_hosts": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def __libraryscraper(self):
        """
        开始刮削媒体库
        """
        if not self._scraper_paths:
            return
        # 排除目录
        exclude_paths = self._exclude_paths.split("\n")
        # 已选择的目录
        paths = self._scraper_paths.split("\n")
        for path in paths:
            if not path:
                continue
            scraper_path = Path(path)
            if not scraper_path.exists():
                logger.warning(f"媒体库刮削路径不存在：{path}")
                continue
            logger.info(f"开始刮削媒体库：{path} ...")
            # 遍历一层文件夹
            for sub_path in scraper_path.iterdir():
                if self._event.is_set():
                    logger.info(f"媒体库刮削服务停止")
                    return
                # 排除目录
                exclude_flag = False
                for exclude_path in exclude_paths:
                    try:
                        if sub_path.is_relative_to(Path(exclude_path)):
                            exclude_flag = True
                            break
                    except Exception as err:
                        print(str(err))
                if exclude_flag:
                    logger.debug(f"{sub_path} 在排除目录中，跳过 ...")
                    continue
                # 开始刮削目录
                if sub_path.is_dir():
                    # 判断目录是不是媒体目录
                    dir_meta = MetaInfo(sub_path.name)
                    if not dir_meta.name or not dir_meta.year:
                        logger.warn(f"{sub_path} 可能不是媒体目录，请检查刮削目录配置，跳过 ...")
                        continue
                    logger.info(f"开始刮削目录：{sub_path} ...")
                    self.__scrape_dir(path=sub_path, dir_meta=dir_meta)
                    logger.info(f"目录 {sub_path} 刮削完成")
            logger.info(f"媒体库 {path} 刮削完成")

    def __scrape_dir(self, path: Path, dir_meta: MetaBase):
        """
        削刮一个目录，该目录必须是媒体文件目录
        """

        # 媒体信息
        mediainfo = None

        # 查找目录下所有的文件
        files = SystemUtils.list_files(path, settings.RMT_MEDIAEXT)
        for file in files:
            if self._event.is_set():
                logger.info(f"媒体库刮削服务停止")
                return

            # 识别元数据
            meta_info = MetaInfo(file.stem)
            # 合并
            meta_info.merge(dir_meta)
            # 是否刮削
            scrap_metadata = settings.SCRAP_METADATA

            # 没有媒体信息或者名字出现变化时，需要重新识别
            if not mediainfo \
                    or meta_info.name != dir_meta.name:
                # 优先读取本地nfo文件
                tmdbid = None
                if meta_info.type == MediaType.MOVIE:
                    # 电影
                    movie_nfo = file.parent / "movie.nfo"
                    if movie_nfo.exists():
                        tmdbid = self.__get_tmdbid_from_nfo(movie_nfo)
                    file_nfo = file.with_suffix(".nfo")
                    if not tmdbid and file_nfo.exists():
                        tmdbid = self.__get_tmdbid_from_nfo(file_nfo)
                else:
                    # 电视剧
                    tv_nfo = file.parent.parent / "tvshow.nfo"
                    if tv_nfo.exists():
                        tmdbid = self.__get_tmdbid_from_nfo(tv_nfo)
                if tmdbid:
                    # 按TMDBID识别
                    logger.info(f"读取到本地nfo文件的tmdbid：{tmdbid}")
                    mediainfo = self.chain.recognize_media(tmdbid=tmdbid, mtype=meta_info.type)
                else:
                    # 按名称识别
                    mediainfo = self.chain.recognize_media(meta=meta_info)
                if not mediainfo:
                    logger.warn(f"未识别到媒体信息：{file}")
                    continue
                    
                # 如果未开启新增已入库媒体是否跟随TMDB信息变化则根据tmdbid查询之前的title
                if not settings.SCRAP_FOLLOW_TMDB:
                    transfer_history = self.transferhis.get_by_type_tmdbid(tmdbid=mediainfo.tmdb_id,
                                                                           mtype=mediainfo.type.value)
                    if transfer_history:
                        mediainfo.title = transfer_history.title

                # 覆盖模式时，提前删除nfo
                if self._mode in ["force_all", "force_nfo"]:
                    scrap_metadata = True
                    nfo_files = SystemUtils.list_files(path, [".nfo"])
                    for nfo_file in nfo_files:
                        try:
                            logger.warn(f"删除nfo文件：{nfo_file}")
                            nfo_file.unlink()
                        except Exception as err:
                            print(str(err))

                # 覆盖模式时，提前删除图片文件
                if self._mode in ["force_all", "force_image"]:
                    scrap_metadata = True
                    image_files = SystemUtils.list_files(path, [".jpg", ".png"])
                    for image_file in image_files:
                        if ".actors" in str(image_file):
                            continue
                        try:
                            logger.warn(f"删除图片文件：{image_file}")
                            image_file.unlink()
                        except Exception as err:
                            print(str(err))

            # 刮削单个文件
            if scrap_metadata:
                self.chain.scrape_metadata(path=file, mediainfo=mediainfo, transfer_type=settings.TRANSFER_TYPE)

    @staticmethod
    def __get_tmdbid_from_nfo(file_path: Path):
        """
        从nfo文件中获取信息
        :param file_path:
        :return: tmdbid
        """
        if not file_path:
            return None
        xpaths = [
            "uniqueid[@type='Tmdb']",
            "uniqueid[@type='tmdb']",
            "uniqueid[@type='TMDB']",
            "tmdbid"
        ]
        reader = NfoReader(file_path)
        for xpath in xpaths:
            try:
                tmdbid = reader.get_element_value(xpath)
                if tmdbid:
                    return tmdbid
            except Exception as err:
                print(str(err))
        return None

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))
