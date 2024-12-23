import platform
import threading
import traceback
from pathlib import Path
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
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.systemconfig_oper import SystemConfigOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.helper.message import MessageHelper
from app.log import logger
from app.schemas import FileItem, TransferTask
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
        self.callback.event_handler(event=event, text="创建", event_path=Path(event.src_path))

    def on_moved(self, event: FileSystemMovedEvent):
        self.callback.event_handler(event=event, text="移动", event_path=Path(event.dest_path))


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

        # 读取目录配置
        monitor_dirs = self.directoryhelper.get_download_dirs()
        if not monitor_dirs:
            return

        # 按下载目录去重
        monitor_dirs = list({f"{d.storage}_{d.download_path}": d for d in monitor_dirs}.values())

        # 启动定时服务进程
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)

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
                    if mon_dir.monitor_mode == "fast":
                        observer = self.__choose_observer()
                    else:
                        observer = PollingObserver()
                    self._observers.append(observer)
                    observer.schedule(FileMonitorHandler(mon_path=mon_path, callback=self),
                                      path=str(mon_path),
                                      recursive=True)
                    observer.daemon = True
                    observer.start()
                    logger.info(f"已启动 {mon_path} 的目录监控服务, 监控模式：{mon_dir.monitor_mode}")
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
                # 远程目录监控
                self._scheduler.add_job(self.polling_observer, 'interval', minutes=self._snapshot_interval,
                                        kwargs={
                                            'storage': mon_dir.storage,
                                            'mon_path': mon_path
                                        })
        # 启动定时服务
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

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
                        self.__handle_file(storage=storage, event_path=Path(new_file))
                # 更新快照
                self._storage_snapshot[storage] = new_snapshot

    def event_handler(self, event, text: str, event_path: Path):
        """
        处理文件变化
        :param event: 事件
        :param text: 事件描述
        :param event_path: 事件文件路径
        """
        if not event.is_directory:
            # 文件发生变化
            logger.debug(f"文件 {event_path} 发生了 {text}")
            # 整理文件
            self.__handle_file(storage="local", event_path=event_path)

    def __transfer_queue(self, task: TransferTask):
        """
        添加到整理队列
        """
        # 加入整理队列，使用默认的回调函数
        self.transferchain.put_to_queue(task=task)

    def __handle_file(self, storage: str, event_path: Path):
        """
        整理一个文件
        :param storage: 存储
        :param event_path: 事件文件路径
        """
        # 全程加锁
        with lock:
            try:
                # 开始整理
                # TODO 缺少文件大小
                self.transferchain.do_transfer(
                    fileitem=FileItem(
                        storage=storage,
                        path=str(event_path),
                        type="file",
                        name=event_path.name,
                        basename=event_path.stem,
                        extension=event_path.suffix[1:]
                    ),
                    src_match=True
                )
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
                    logger.info(f"正在停止目录监控服务：{observer}...")
                    observer.stop()
                    observer.join()
                    logger.info(f"{observer} 目录监控已停止")
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
