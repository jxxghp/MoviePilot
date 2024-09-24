import platform
import re
import threading
import traceback
from pathlib import Path
from queue import Queue
from threading import Lock
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.events import FileSystemEventHandler, FileSystemMovedEvent, FileSystemEvent
from watchdog.observers.polling import PollingObserver

from app.chain import ChainBase
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.tmdb import TmdbChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import EventManager
from app.core.metainfo import MetaInfoPath
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.systemconfig_oper import SystemConfigOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.helper.message import MessageHelper
from app.log import logger
from app.schemas import FileItem, TransferInfo, Notification
from app.schemas.types import SystemConfigKey, MediaType, NotificationType, EventType
from app.utils.singleton import Singleton

lock = Lock()
snapshot_lock = Lock()


class MonitorChain(ChainBase):
    pass


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, mon_path: Path, callback: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = mon_path
        self.callback = callback

    def on_created(self, event: FileSystemEvent):
        self.callback.event_handler(event=event, text="创建",
                                    mon_path=self._watch_path, event_path=Path(event.src_path))

    def on_moved(self, event: FileSystemMovedEvent):
        self.callback.event_handler(event=event, text="移动",
                                    mon_path=self._watch_path, event_path=Path(event.dest_path))


class Monitor(metaclass=Singleton):
    """
    目录监控处理链，单例模式
    """

    # 退出事件
    _event = threading.Event()

    # 监控服务
    _observers = []

    # 定时服务
    _scheduler = None

    # 存储快照
    _storage_snapshot = {}

    # 存储过照间隔（分钟）
    _snapshot_interval = 5

    # 待整理任务队列
    _queue = Queue()

    # 文件整理线程
    _transfer_thread = None

    # 文件整理间隔（秒）
    _transfer_interval = 60

    def __init__(self):
        super().__init__()
        self.chain = MonitorChain()
        self.transferhis = TransferHistoryOper()
        self.transferchain = TransferChain()
        self.downloadhis = DownloadHistoryOper()
        self.mediaChain = MediaChain()
        self.tmdbchain = TmdbChain()
        self.storagechain = StorageChain()
        self.directoryhelper = DirectoryHelper()
        self.systemmessage = MessageHelper()
        self.systemconfig = SystemConfigOper()

        self.all_exts = settings.RMT_MEDIAEXT

        # 启动目录监控和文件整理
        self.init()

    def init(self):
        """
        启动监控
        """
        # 停止现有任务
        self.stop()

        # 启动文件整理线程
        self._transfer_thread = threading.Thread(target=self.__start_transfer)
        self._transfer_thread.start()

        # 读取目录配置
        monitor_dirs = self.directoryhelper.get_download_dirs()
        if not monitor_dirs:
            return
        for mon_dir in monitor_dirs:
            if not mon_dir.library_path:
                continue
            if mon_dir.monitor_type != "monitor":
                continue
            # 检查媒体库目录是不是下载目录的子目录
            mon_path = Path(mon_dir.download_path)
            target_path = Path(mon_dir.library_path)
            if target_path.is_relative_to(mon_path):
                logger.warn(f"{target_path} 是监控目录 {mon_path} 的子目录，无法监控！")
                self.systemmessage.put(f"{target_path} 是监控目录 {mon_path} 的子目录，无法监控", title="目录监控")
                continue

            # 启动监控
            if mon_dir.storage == "local":
                # 本地目录监控
                try:
                    observer = self.__choose_observer()
                    self._observers.append(observer)
                    observer.schedule(FileMonitorHandler(mon_path=mon_path, callback=self),
                                      path=mon_path,
                                      recursive=True)
                    observer.daemon = True
                    observer.start()
                    logger.info(f"已启动 {mon_path} 的目录监控服务")
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
                    self.systemmessage.put(f"{mon_path} 启动目录监控失败：{err_msg}", title="目录监控")
            else:
                # 启动定时服务进程
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                # 远程目录监控
                self._scheduler.add_job(self.polling_observer, 'interval', minutes=self._snapshot_interval,
                                        kwargs={
                                            'storage': mon_dir.storage,
                                            'mon_path': mon_path
                                        })

    @staticmethod
    def __choose_observer() -> Any:
        """
        选择最优的监控模式
        """
        system = platform.system()

        try:
            if system == 'Linux':
                from watchdog.observers.inotify import InotifyObserver
                return InotifyObserver()
            elif system == 'Darwin':
                from watchdog.observers.fsevents import FSEventsObserver
                return FSEventsObserver()
            elif system == 'Windows':
                from watchdog.observers.read_directory_changes import WindowsApiObserver
                return WindowsApiObserver()
        except Exception as error:
            logger.warn(f"导入模块错误：{error}，将使用 PollingObserver 监控目录")
        return PollingObserver()

    def put_to_queue(self, storage: str, filepath: Path, mon_path: Path):
        """
        添加到待整理队列
        """
        self._queue.put({
            "storage": storage,
            "filepath": filepath,
            "mon_path": mon_path
        })

    def polling_observer(self, storage: str, mon_path: Path):
        """
        轮询监控
        """
        with snapshot_lock:
            # 快照存储
            new_snapshot = self.storagechain.snapshot_storage(storage=storage, path=mon_path)
            if new_snapshot:
                # 比较快照
                old_snapshot = self._storage_snapshot.get(storage)
                if old_snapshot:
                    # 新增的文件
                    new_files = new_snapshot.keys() - old_snapshot.keys()
                    for new_file in new_files:
                        # 添加到待整理队列
                        self.put_to_queue(storage=storage, filepath=Path(new_file), mon_path=mon_path)
                # 更新快照
                self._storage_snapshot[storage] = new_snapshot

    def event_handler(self, event, mon_path: Path, text: str, event_path: Path):
        """
        处理文件变化
        :param event: 事件
        :param mon_path: 监控目录
        :param text: 事件描述
        :param event_path: 事件文件路径
        """
        if not event.is_directory:
            # 文件发生变化
            logger.debug(f"文件 {event_path} 发生了 {text}")
            # 添加到待整理队列
            self.put_to_queue(storage="local", filepath=event_path, mon_path=mon_path)

    def __start_transfer(self):
        """
        整理队列中的文件
        """
        while not self._event.is_set():
            try:
                item = self._queue.get(timeout=self._transfer_interval)
                if item:
                    self.__handle_file(storage=item.get("storage"),
                                       event_path=item.get("filepath"),
                                       mon_path=item.get("mon_path"))
            except TimeoutError:
                continue
            except Exception as e:
                logger.error(f"整理队列处理出现错误：{e}")

    def __handle_file(self, storage: str, event_path: Path, mon_path: Path):
        """
        整理一个文件
        :param event_path: 事件文件路径
        :param mon_path: 监控目录
        """

        def __get_bluray_dir(_path: Path):
            """
            获取BDMV目录的上级目录
            """
            for parent in _path.parents:
                if parent.name == "BDMV":
                    return parent.parent
            return None

        # 全程加锁
        with lock:
            try:
                # 回收站及隐藏的文件不处理
                if str(event_path).find('/@Recycle/') != -1 \
                        or str(event_path).find('/#recycle/') != -1 \
                        or str(event_path).find('/.') != -1 \
                        or str(event_path).find('/@eaDir') != -1:
                    logger.debug(f"{event_path} 是回收站或隐藏的文件")
                    return

                # 不是媒体文件不处理
                if event_path.suffix.lower() not in self.all_exts:
                    logger.debug(f"{event_path} 不是媒体文件")
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

                # 判断是不是蓝光目录
                bluray_flag = False
                if re.search(r"BDMV[/\\]STREAM", str(event_path), re.IGNORECASE):
                    bluray_flag = True
                    # 截取BDMV前面的路径
                    event_path = __get_bluray_dir(event_path)
                    logger.info(f"{event_path} 是蓝光原盘目录，更正文件路径为：{event_path}")

                # 查询历史记录，已转移的不处理
                if self.transferhis.get_by_src(str(event_path)):
                    logger.info(f"{event_path} 已经整理过了")
                    return

                # 元数据
                file_meta = MetaInfoPath(event_path)
                if not file_meta.name:
                    logger.error(f"{event_path.name} 无法识别有效信息")
                    return

                # 根据父路径获取下载历史
                download_history = None
                if bluray_flag:
                    # 蓝光原盘，按目录名查询
                    download_history = self.downloadhis.get_by_path(str(event_path))
                else:
                    # 按文件全路径查询
                    download_file = self.downloadhis.get_file_by_fullpath(str(event_path))
                    if download_file:
                        download_history = self.downloadhis.get_by_hash(download_file.download_hash)

                # 获取下载Hash
                download_hash = None
                if download_history:
                    download_hash = download_history.download_hash

                # 识别媒体信息
                if download_history and download_history.tmdbid:
                    mediainfo: MediaInfo = self.mediaChain.recognize_media(mtype=MediaType(download_history.type),
                                                                           tmdbid=download_history.tmdbid,
                                                                           doubanid=download_history.doubanid)
                else:
                    mediainfo: MediaInfo = self.mediaChain.recognize_by_meta(file_meta)
                if not mediainfo:
                    logger.warn(f'未识别到媒体信息，标题：{file_meta.name}')
                    # 新增转移失败历史记录
                    his = self.transferhis.add_fail(
                        fileitem=FileItem(
                            storage=storage,
                            type="file",
                            path=str(event_path),
                            name=event_path.name,
                            basename=event_path.stem,
                            extension=event_path.suffix[1:],
                        ),
                        mode='',
                        meta=file_meta,
                        download_hash=download_hash
                    )
                    self.chain.post_message(Notification(
                        mtype=NotificationType.Manual,
                        title=f"{event_path.name} 未识别到媒体信息，无法入库！",
                        text=f"回复：```\n/redo {his.id} [tmdbid]|[类型]\n``` 手动识别转移。",
                        link=settings.MP_DOMAIN('#/history')
                    ))
                    return

                # 查询转移目的目录
                dir_info = self.directoryhelper.get_dir(mediainfo, src_path=Path(mon_path))
                if not dir_info:
                    logger.warn(f"{event_path.name} 未找到对应的目标目录")
                    return

                # 查找这个文件项
                file_item = self.storagechain.get_file_item(storage=storage, path=event_path)
                if not file_item:
                    logger.warn(f"{event_path.name} 未找到对应的文件")
                    return

                # 如果未开启新增已入库媒体是否跟随TMDB信息变化则根据tmdbid查询之前的title
                if not settings.SCRAP_FOLLOW_TMDB:
                    transfer_history = self.transferhis.get_by_type_tmdbid(tmdbid=mediainfo.tmdb_id,
                                                                           mtype=mediainfo.type.value)
                    if transfer_history:
                        mediainfo.title = transfer_history.title
                logger.info(f"{event_path.name} 识别为：{mediainfo.type.value} {mediainfo.title_year}")

                # 更新媒体图片
                self.chain.obtain_images(mediainfo=mediainfo)

                # 获取集数据
                if mediainfo.type == MediaType.TV:
                    episodes_info = self.tmdbchain.tmdb_episodes(tmdbid=mediainfo.tmdb_id,
                                                                 season=file_meta.begin_season or 1)
                else:
                    episodes_info = None

                # 转移
                transferinfo: TransferInfo = self.chain.transfer(fileitem=file_item,
                                                                 meta=file_meta,
                                                                 mediainfo=mediainfo,
                                                                 transfer_type=dir_info.transfer_type,
                                                                 target_storage=dir_info.library_storage,
                                                                 target_path=Path(dir_info.library_path),
                                                                 episodes_info=episodes_info,
                                                                 scrape=dir_info.scraping)

                if not transferinfo:
                    logger.error("文件转移模块运行失败")
                    return

                if not transferinfo.success:
                    # 转移失败
                    logger.warn(f"{event_path.name} 入库失败：{transferinfo.message}")
                    # 新增转移失败历史记录
                    self.transferhis.add_fail(
                        fileitem=file_item,
                        mode=dir_info.transfer_type,
                        download_hash=download_hash,
                        meta=file_meta,
                        mediainfo=mediainfo,
                        transferinfo=transferinfo
                    )
                    # 发送失败消息
                    self.chain.post_message(Notification(
                        mtype=NotificationType.Manual,
                        title=f"{mediainfo.title_year} {file_meta.season_episode} 入库失败！",
                        text=f"原因：{transferinfo.message or '未知'}",
                        image=mediainfo.get_message_image(),
                        link=settings.MP_DOMAIN('#/history')
                    ))
                    return

                # TODO 汇总刮削
                if dir_info.scraping:
                    self.mediaChain.scrape_metadata(fileitem=transferinfo.target_diritem,
                                                    meta=file_meta,
                                                    mediainfo=mediainfo)

                # 广播事件
                EventManager().send_event(EventType.TransferComplete, {
                    'fileitem': file_item,
                    'meta': file_meta,
                    'mediainfo': mediainfo,
                    'transferinfo': transferinfo
                })

                # TODO 汇总发送成功消息
                self.transferchain.send_transfer_message(meta=file_meta,
                                                         mediainfo=mediainfo,
                                                         transferinfo=transferinfo)

                # 移动模式删除空目录
                if dir_info.transfer_type in ["move"]:
                    logger.info(f"正在删除： {file_item.storage} {file_item.path}")
                    self.storagechain.delete_file(file_item)

            except Exception as e:
                logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def stop(self):
        """
        退出插件
        """
        self._event.set()
        if self._observers:
            for observer in self._observers:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    logger.error(f"停止目录监控服务出现了错误：{e}")
            self._observers = []
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                try:
                    self._scheduler.shutdown()
                except Exception as e:
                    logger.error(f"停止定时服务出现了错误：{e}")
            self._scheduler = None
        self._event.clear()
