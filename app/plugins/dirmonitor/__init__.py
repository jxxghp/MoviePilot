import re
import threading
import traceback
from pathlib import Path
from typing import List, Tuple, Dict, Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType, Notification, NotificationType, TransferInfo
from app.schemas.types import EventType

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
        self.sync.file_change_handler(event, "创建", event.src_path)

    def on_moved(self, event):
        self.sync.file_change_handler(event, "移动", event.dest_path)


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

    # 已处理的文件清单
    _synced_files = []

    # 私有属性
    transferhis = None
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

    def init_plugin(self, config: dict = None):
        self.transferhis = TransferHistoryOper()
        self.transferchian = TransferChain()

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
            # 启动任务
            monitor_dirs = self._monitor_dirs.split("\n")
            if not monitor_dirs:
                return
            for mon_path in monitor_dirs:
                if not mon_path:
                    continue
                # 检查目录是不是媒体库目录的子目录
                if Path(mon_path).is_relative_to(settings.LIBRARY_PATH):
                    logger.warn(f"{mon_path} 是媒体库目录的子目录，无法监控")
                    self.systemmessage.put(f"{mon_path} 是媒体库目录的子目录，无法监控")
                    continue

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
                        logger.warn(f"目录监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                                    + """
                                 echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                 echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                 sudo sysctl -p
                                 """)
                    else:
                        logger.error(f"{mon_path} 启动目录监控失败：{err_msg}")
                    self.systemmessage.put(f"{mon_path} 启动目录监控失败：{err_msg}")

    def file_change_handler(self, event, text: str, event_path: str):
        """
        处理文件变化
        :param event: 事件
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
                    if event_path not in self._synced_files:
                        self._synced_files.append(event_path)
                    else:
                        logger.debug("文件已处理过：%s" % event_path)
                        return

                    # 命中过滤关键字不处理
                    if self._exclude_keywords:
                        for keyword in self._exclude_keywords.split("\n"):
                            if keyword and re.findall(keyword, event_path):
                                logger.debug(f"{event_path} 命中过滤关键字 {keyword}")
                                return

                    # 回收站及隐藏的文件不处理
                    if event_path.find('/@Recycle/') != -1 \
                            or event_path.find('/#recycle/') != -1 \
                            or event_path.find('/.') != -1 \
                            or event_path.find('/@eaDir') != -1:
                        logger.debug(f"{event_path} 是回收站或隐藏的文件")
                        return

                    # 不是媒体文件不处理
                    if file_path.suffix not in settings.RMT_MEDIAEXT:
                        logger.debug(f"{event_path} 不是媒体文件")
                        return

                    # 查询历史记录，已转移的不处理
                    if self.transferhis.get_by_src(event_path):
                        logger.info(f"{event_path} 已整理过")
                        return

                    # 文件元数据
                    file_meta = MetaInfo(title=file_path.name)
                    # 上级目录元数据
                    dir_meta = MetaInfo(title=file_path.parent.name)
                    # 整合元数据
                    if not file_meta.cn_name and dir_meta.cn_name:
                        file_meta.cn_name = dir_meta.cn_name
                    if not file_meta.en_name and dir_meta.en_name:
                        file_meta.en_name = dir_meta.en_name
                    if file_meta.type != MediaType.TV and dir_meta.type == MediaType.TV:
                        file_meta.type = MediaType.TV
                    if not file_meta.year and dir_meta.year:
                        file_meta.year = dir_meta.year
                    if not file_meta.begin_season and dir_meta.begin_season:
                        file_meta.begin_season = dir_meta.begin_season
                    if not file_meta.episode_list and dir_meta.episode_list:
                        file_meta.begin_episode = dir_meta.begin_episode
                        file_meta.end_episode = dir_meta.end_episode

                    if not file_meta.name:
                        logger.warn(f"{file_path.name} 无法识别有效信息")
                        return

                    # 识别媒体信息
                    mediainfo: MediaInfo = self.chain.recognize_media(meta=file_meta)
                    if not mediainfo:
                        logger.warn(f'未识别到媒体信息，标题：{file_meta.name}')
                        if self._notify:
                            self.chain.post_message(Notification(
                                mtype=NotificationType.Manual,
                                title=f"{file_path.name} 未识别到媒体信息，无法入库！"
                            ))
                        return
                    logger.info(f"{file_path.name} 识别为：{mediainfo.type.value} {mediainfo.title_year}")

                    # 更新媒体图片
                    self.chain.obtain_images(mediainfo=mediainfo)

                    # 转移
                    transferinfo: TransferInfo = self.chain.transfer(mediainfo=mediainfo,
                                                                     path=file_path,
                                                                     transfer_type=self._transfer_type)

                    if not transferinfo or not transferinfo.target_path:
                        # 转移失败
                        logger.warn(f"{file_path.name} 入库失败")
                        if self._notify:
                            self.chain.post_message(Notification(
                                title=f"{mediainfo.title_year}{file_meta.season_episode} 入库失败！",
                                text=f"原因：{transferinfo.message if transferinfo else '未知'}",
                                image=mediainfo.get_message_image()
                            ))
                        return

                    # 新增转移成功历史记录
                    self.transferhis.add(
                        src=event_path,
                        dest=str(transferinfo.target_path) if transferinfo else None,
                        mode=settings.TRANSFER_TYPE,
                        type=mediainfo.type.value,
                        category=mediainfo.category,
                        title=mediainfo.title,
                        year=mediainfo.year,
                        tmdbid=mediainfo.tmdb_id,
                        imdbid=mediainfo.imdb_id,
                        tvdbid=mediainfo.tvdb_id,
                        doubanid=mediainfo.douban_id,
                        seasons=file_meta.season,
                        episodes=file_meta.episode,
                        image=mediainfo.get_poster_image(),
                        status=1
                    )

                    # 刮削元数据
                    self.chain.scrape_metadata(path=transferinfo.target_path, mediainfo=mediainfo)
                    # 刷新媒体库
                    self.chain.refresh_mediaserver(mediainfo=mediainfo, file_path=transferinfo.target_path)
                    # 发送通知
                    if self._notify:
                        self.transferchian.send_transfer_message(meta=file_meta, mediainfo=mediainfo, transferinfo=transferinfo)
                    # 广播事件
                    self.eventmanager.send_event(EventType.TransferComplete, {
                        'meta': file_meta,
                        'mediainfo': mediainfo,
                        'transferinfo': transferinfo
                    })

            except Exception as e:
                logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

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
                                            'placeholder': '每一行一个目录'
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
