import re
import shutil
import threading
import traceback
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import List, Tuple, Dict, Any

from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import Notification, NotificationType, TransferInfo
from app.schemas.types import EventType, MediaType, SystemConfigKey
from app.utils.string import StringUtils
from app.utils.system import SystemUtils

lock = threading.Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, monpath: str, sync: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = monpath
        self.sync = sync

    def on_created(self, event):
        self.sync.event_handler(event=event, text="创建",
                                mon_path=self._watch_path, event_path=event.src_path)

    def on_moved(self, event):
        self.sync.event_handler(event=event, text="移动",
                                mon_path=self._watch_path, event_path=event.dest_path)


class DirMonitor(_PluginBase):
    # 插件名称
    plugin_name = "目录监控"
    # 插件描述
    plugin_desc = "监控目录文件发生变化时实时整理到媒体库。"
    # 插件图标
    plugin_icon = "directory.png"
    # 主题色
    plugin_color = "#E0995E"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "dirmonitor_"
    # 加载顺序
    plugin_order = 4
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    transferhis = None
    downloadhis = None
    transferchian = None
    _observer = []
    _enabled = False
    _notify = False
    # 模式 compatibility/fast
    _mode = "fast"
    # 转移方式
    _transfer_type = settings.TRANSFER_TYPE
    _monitor_dirs = ""
    _exclude_keywords = ""
    # 存储源目录与目的目录关系
    _dirconf: Dict[str, Path] = {}
    _medias = {}
    # 退出事件
    _event = Event()

    def init_plugin(self, config: dict = None):
        self.transferhis = TransferHistoryOper(self.db)
        self.downloadhis = DownloadHistoryOper(self.db)
        self.transferchian = TransferChain(self.db)

        # 清空配置
        self._dirconf = {}

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._mode = config.get("mode")
            self._transfer_type = config.get("transfer_type")
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""

        # 停止现有任务
        self.stop_service()

        if self._enabled:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            # 启动任务
            monitor_dirs = self._monitor_dirs.split("\n")
            if not monitor_dirs:
                return
            for mon_path in monitor_dirs:
                # 格式源目录:目的目录
                if not mon_path:
                    continue

                # 存储目的目录
                if SystemUtils.is_windows():
                    if mon_path.count(":") > 1:
                        paths = [mon_path.split(":")[0] + ":" + mon_path.split(":")[1],
                                 mon_path.split(":")[2] + ":" + mon_path.split(":")[3]]
                    else:
                        paths = [mon_path]
                else:
                    paths = mon_path.split(":")
                target_path = None
                if len(paths) > 1:
                    mon_path = paths[0]
                    target_path = Path(paths[1])
                    self._dirconf[mon_path] = target_path

                # 检查媒体库目录是不是下载目录的子目录
                try:
                    if target_path.is_relative_to(Path(mon_path)):
                        logger.warn(f"{target_path} 是下载目录 {mon_path} 的子目录，无法监控")
                        self.systemmessage.put(f"{target_path} 是下载目录 {mon_path} 的子目录，无法监控")
                        continue
                except Exception as e:
                    logger.debug(str(e))
                    pass

                try:
                    if self._mode == "compatibility":
                        # 兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB
                        observer = PollingObserver(timeout=10)
                    else:
                        # 内部处理系统操作类型选择最优解
                        observer = Observer(timeout=10)
                    self._observer.append(observer)
                    observer.schedule(FileMonitorHandler(mon_path, self), path=mon_path, recursive=True)
                    observer.daemon = True
                    observer.start()
                    logger.info(f"{mon_path} 的目录监控服务启动")
                except Exception as e:
                    err_msg = str(e)
                    if "inotify" in err_msg and "reached" in err_msg:
                        logger.warn(
                            f"目录监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                            + """
                                 echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                 echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                 sudo sysctl -p
                                 """)
                    else:
                        logger.error(f"{mon_path} 启动目录监控失败：{err_msg}")
                    self.systemmessage.put(f"{mon_path} 启动目录监控失败：{err_msg}")

            # 追加入库消息统一发送服务
            self._scheduler.add_job(self.send_msg, trigger='interval', seconds=15)
            # 启动服务
            self._scheduler.print_jobs()
            self._scheduler.start()

    def event_handler(self, event, mon_path: str, text: str, event_path: str):
        """
        处理文件变化
        :param event: 事件
        :param mon_path: 监控目录
        :param text: 事件描述
        :param event_path: 事件文件路径
        """
        if not event.is_directory:
            # 文件发生变化
            file_path = Path(event_path)
            try:
                if not file_path.exists():
                    return

                logger.debug("文件%s：%s" % (text, event_path))

                # 全程加锁
                with lock:
                    transfer_history = self.transferhis.get_by_src(event_path)
                    if transfer_history:
                        logger.debug("文件已处理过：%s" % event_path)
                        return

                    # 回收站及隐藏的文件不处理
                    if event_path.find('/@Recycle/') != -1 \
                            or event_path.find('/#recycle/') != -1 \
                            or event_path.find('/.') != -1 \
                            or event_path.find('/@eaDir') != -1:
                        logger.debug(f"{event_path} 是回收站或隐藏的文件")
                        return

                    # 命中过滤关键字不处理
                    if self._exclude_keywords:
                        for keyword in self._exclude_keywords.split("\n"):
                            if keyword and re.findall(keyword, event_path):
                                logger.info(f"{event_path} 命中过滤关键字 {keyword}，不处理")
                                return

                    # 整理屏蔽词不处理
                    transfer_exclude_words = self.systemconfig.get(SystemConfigKey.TransferExcludeWords)
                    if transfer_exclude_words:
                        for keyword in transfer_exclude_words:
                            if not keyword:
                                continue
                            if keyword and re.search(r"%s" % keyword, event_path, re.IGNORECASE):
                                logger.info(f"{event_path} 命中整理屏蔽词 {keyword}，不处理")
                                return

                    # 不是媒体文件不处理
                    if file_path.suffix not in settings.RMT_MEDIAEXT:
                        logger.debug(f"{event_path} 不是媒体文件")
                        return

                    # 查询历史记录，已转移的不处理
                    if self.transferhis.get_by_src(event_path):
                        logger.info(f"{event_path} 已整理过")
                        return

                    # 上级目录元数据
                    meta = MetaInfo(title=file_path.parent.name)
                    # 文件元数据，不包含后缀
                    file_meta = MetaInfo(title=file_path.stem)
                    # 合并元数据
                    file_meta.merge(meta)

                    if not file_meta.name:
                        logger.error(f"{file_path.name} 无法识别有效信息")
                        return

                    # 查询转移目的目录
                    target: Path = self._dirconf.get(mon_path)

                    # 识别媒体信息
                    mediainfo: MediaInfo = self.chain.recognize_media(meta=file_meta)
                    if not mediainfo:
                        logger.warn(f'未识别到媒体信息，标题：{file_meta.name}')
                        if self._notify:
                            self.chain.post_message(Notification(
                                mtype=NotificationType.Manual,
                                title=f"{file_path.name} 未识别到媒体信息，无法入库！"
                            ))
                        # 新增转移成功历史记录
                        self.transferhis.add_fail(
                            src_path=file_path,
                            mode=self._transfer_type,
                            meta=file_meta
                        )
                        return

                    # 如果未开启新增已入库媒体是否跟随TMDB信息变化则根据tmdbid查询之前的title
                    if not settings.SCRAP_FOLLOW_TMDB:
                        transfer_history = self.transferhis.get_by_type_tmdbid(tmdbid=mediainfo.tmdb_id,
                                                                               mtype=mediainfo.type.value)
                        if transfer_history:
                            mediainfo.title = transfer_history.title
                    logger.info(f"{file_path.name} 识别为：{mediainfo.type.value} {mediainfo.title_year}")

                    # 更新媒体图片
                    self.chain.obtain_images(mediainfo=mediainfo)

                    # 获取downloadhash
                    download_hash = self.get_download_hash(src=str(file_path))

                    # 转移
                    transferinfo: TransferInfo = self.chain.transfer(mediainfo=mediainfo,
                                                                     path=file_path,
                                                                     transfer_type=self._transfer_type,
                                                                     target=target,
                                                                     meta=file_meta)

                    if not transferinfo:
                        logger.error("文件转移模块运行失败")
                        return
                    if not transferinfo.success:
                        # 转移失败
                        logger.warn(f"{file_path.name} 入库失败：{transferinfo.message}")
                        # 新增转移失败历史记录
                        self.transferhis.add_fail(
                            src_path=file_path,
                            mode=self._transfer_type,
                            download_hash=download_hash,
                            meta=file_meta,
                            mediainfo=mediainfo,
                            transferinfo=transferinfo
                        )
                        if self._notify:
                            self.chain.post_message(Notification(
                                title=f"{mediainfo.title_year}{file_meta.season_episode} 入库失败！",
                                text=f"原因：{transferinfo.message or '未知'}",
                                image=mediainfo.get_message_image()
                            ))
                        return

                    # 新增转移成功历史记录
                    self.transferhis.add_success(
                        src_path=file_path,
                        mode=self._transfer_type,
                        download_hash=download_hash,
                        meta=file_meta,
                        mediainfo=mediainfo,
                        transferinfo=transferinfo
                    )

                    # 刮削单个文件
                    if settings.SCRAP_METADATA:
                        self.chain.scrape_metadata(path=transferinfo.target_path,
                                                   mediainfo=mediainfo)

                    """
                    {
                        "title_year season": {
                            "files": [
                                {
                                    "path":,
                                    "mediainfo":,
                                    "file_meta":,
                                    "transferinfo":
                                }
                            ],
                            "time": "2023-08-24 23:23:23.332"
                        }
                    }
                    """
                    # 发送消息汇总
                    media_list = self._medias.get(mediainfo.title_year + " " + meta.season) or {}
                    if media_list:
                        media_files = media_list.get("files") or []
                        if media_files:
                            file_exists = False
                            for file in media_files:
                                if str(event_path) == file.get("path"):
                                    file_exists = True
                                    break
                            if not file_exists:
                                media_files.append({
                                    "path": event_path,
                                    "mediainfo": mediainfo,
                                    "file_meta": file_meta,
                                    "transferinfo": transferinfo
                                })
                            else:
                                media_files = [
                                    {
                                        "path": event_path,
                                        "mediainfo": mediainfo,
                                        "file_meta": file_meta,
                                        "transferinfo": transferinfo
                                    }
                                ]
                        media_list = {
                            "files": media_files,
                            "time": datetime.now()
                        }
                    else:
                        media_list = {
                            "files": [
                                {
                                    "path": event_path,
                                    "mediainfo": mediainfo,
                                    "file_meta": file_meta,
                                    "transferinfo": transferinfo
                                }
                            ],
                            "time": datetime.now()
                        }
                    self._medias[mediainfo.title_year + " " + meta.season] = media_list

                    # 汇总刷新媒体库
                    if settings.REFRESH_MEDIASERVER:
                        self.chain.refresh_mediaserver(mediainfo=mediainfo, file_path=transferinfo.target_path)
                    # 广播事件
                    self.eventmanager.send_event(EventType.TransferComplete, {
                        'meta': file_meta,
                        'mediainfo': mediainfo,
                        'transferinfo': transferinfo
                    })

                    # 移动模式删除空目录
                    if self._transfer_type == "move":
                        for file_dir in file_path.parents:
                            if len(str(file_dir)) <= len(str(Path(mon_path))):
                                # 重要，删除到监控目录为止
                                break
                            files = SystemUtils.list_files(file_dir, settings.RMT_MEDIAEXT)
                            if not files:
                                logger.warn(f"移动模式，删除空目录：{file_dir}")
                                shutil.rmtree(file_dir, ignore_errors=True)

            except Exception as e:
                logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if not self._medias or not self._medias.keys():
            return

        # 遍历检查是否已刮削完，发送消息
        for medis_title_year_season in list(self._medias.keys()):
            media_list = self._medias.get(medis_title_year_season)
            logger.info(f"开始处理媒体 {medis_title_year_season} 消息")

            if not media_list:
                continue

            # 获取最后更新时间
            last_update_time = media_list.get("time")
            media_files = media_list.get("files")
            if not last_update_time or not media_files:
                continue

            transferinfo = media_files[0].get("transferinfo")
            file_meta = media_files[0].get("file_meta")
            mediainfo = media_files[0].get("mediainfo")
            # 判断最后更新时间距现在是已超过5秒，超过则发送消息
            if (datetime.now() - last_update_time).total_seconds() > 5:
                # 发送通知
                if self._notify:

                    # 汇总处理文件总大小
                    total_size = 0
                    file_count = 0

                    # 剧集汇总
                    episodes = []
                    for file in media_files:
                        transferinfo = file.get("transferinfo")
                        total_size += transferinfo.total_size
                        file_count += 1

                        file_meta = file.get("file_meta")
                        if file_meta and file_meta.begin_episode:
                            episodes.append(file_meta.begin_episode)

                    transferinfo.total_size = total_size
                    # 汇总处理文件数量
                    transferinfo.file_count = file_count

                    # 剧集季集信息 S01 E01-E04 || S01 E01、E02、E04
                    season_episode = None
                    # 处理文件多，说明是剧集，显示季入库消息
                    if mediainfo.type == MediaType.TV:
                        # 季集文本
                        season_episode = f"{file_meta.season} {StringUtils.format_ep(episodes)}"
                    # 发送消息
                    self.transferchian.send_transfer_message(meta=file_meta,
                                                             mediainfo=mediainfo,
                                                             transferinfo=transferinfo,
                                                             season_episode=season_episode)
                # 发送完消息，移出key
                del self._medias[medis_title_year_season]
                continue

    def get_download_hash(self, src: str):
        """
        从表中获取download_hash，避免连接下载器
        """
        downloadHis = self.downloadhis.get_file_by_fullpath(src)
        if downloadHis:
            return downloadHis.download_hash
        return None

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
                                            'model': 'notify',
                                            'label': '发送通知',
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
                                            'label': '监控模式',
                                            'items': [
                                                {'title': '兼容模式', 'value': 'compatibility'},
                                                {'title': '性能模式', 'value': 'fast'}
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
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'transfer_type',
                                            'label': '转移方式',
                                            'items': [
                                                {'title': '移动', 'value': 'move'},
                                                {'title': '复制', 'value': 'copy'},
                                                {'title': '硬链接', 'value': 'link'},
                                                {'title': '软链接', 'value': 'softlink'}
                                            ]
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
                                            'model': 'monitor_dirs',
                                            'label': '监控目录',
                                            'rows': 5,
                                            'placeholder': '每一行一个目录，支持两种配置方式：\n'
                                                           '监控目录\n'
                                                           '监控目录:转移目的目录'
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
                                            'model': 'exclude_keywords',
                                            'label': '排除关键词',
                                            'rows': 2,
                                            'placeholder': '每一行一个关键词'
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
            "notify": False,
            "mode": "fast",
            "transfer_type": settings.TRANSFER_TYPE,
            "monitor_dirs": "",
            "exclude_keywords": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    print(str(e))
        self._observer = []
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._event.set()
                self._scheduler.shutdown()
                self._event.clear()
            self._scheduler = None
