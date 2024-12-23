import datetime
import platform
import re
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
from app.core.context import MediaInfo
from app.core.event import EventManager
from app.core.metainfo import MetaInfoPath
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.systemconfig_oper import SystemConfigOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.helper.message import MessageHelper
from app.log import logger
from app.schemas import FileItem, TransferInfo, Notification, TransferTask
from app.schemas.types import SystemConfigKey, MediaType, NotificationType, EventType
from app.utils.singleton import Singleton
from app.utils.string import StringUtils

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

    # 消息汇总
    _msg_medias = {}

    # 消息汇总间隔（秒）
    _msg_interval = 60

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

        # 追加入库消息统一发送服务
        self._scheduler.add_job(self.__send_msg, trigger='interval', seconds=15)
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

        def __callback(_task: TransferTask, _transferinfo: TransferInfo, /):
            """
            整理完成后处理
            """
            if not _transferinfo:
                logger.error("文件转移模块运行失败")
                return

            if not _transferinfo.success:
                # 转移失败
                logger.warn(f"{_task.file_path.name} 入库失败：{_transferinfo.message}")
                # 新增转移失败历史记录
                self.transferhis.add_fail(
                    fileitem=_task.fileitem,
                    mode=_transferinfo.transfer_type if _transferinfo else '',
                    download_hash=_task.download_hash,
                    meta=_task.meta,
                    mediainfo=_task.mediainfo,
                    transferinfo=_transferinfo
                )
                # 发送失败消息
                self.chain.post_message(Notification(
                    mtype=NotificationType.Manual,
                    title=f"{_task.mediainfo.title_year} {_task.meta.season_episode} 入库失败！",
                    text=f"原因：{_transferinfo.message or '未知'}",
                    image=_task.mediainfo.get_message_image(),
                    link=settings.MP_DOMAIN('#/history')
                ))
                return

            # 转移成功
            logger.info(f"{_task.file_path.name} 入库成功：{_transferinfo.target_diritem.path}")
            # 新增转移成功历史记录
            self.transferhis.add_success(
                fileitem=_task.fileitem,
                mode=_transferinfo.transfer_type if _transferinfo else '',
                download_hash=_task.download_hash,
                meta=_task.meta,
                mediainfo=_task.mediainfo,
                transferinfo=_transferinfo
            )

            # 汇总刮削
            if _transferinfo.need_scrape:
                self.mediaChain.scrape_metadata(fileitem=_transferinfo.target_diritem,
                                                meta=_task.meta,
                                                mediainfo=_task.mediainfo)

            # 广播事件
            EventManager().send_event(EventType.TransferComplete, {
                'fileitem': _task.fileitem,
                'meta': _task.meta,
                'mediainfo': _task.mediainfo,
                'transferinfo': _transferinfo
            })

            # 发送消息汇总
            if _transferinfo.need_notify:
                self.__collect_msg_medias(mediainfo=_task.mediainfo, file_meta=_task.meta,
                                          transferinfo=_transferinfo)

            # 移动模式删除空目录
            if _transferinfo.transfer_type in ["move"]:
                self.storagechain.delete_media_file(_task.fileitem, delete_self=False)

        # 加入整理队列
        self.transferchain.put_to_queue(task=task, callback=__callback)

    def __handle_file(self, storage: str, event_path: Path):
        """
        整理一个文件
        :param storage: 存储
        :param event_path: 事件文件路径
        """

        def __get_bluray_dir(_path: Path):
            """
            获取BDMV目录的上级目录
            """
            for p in _path.parents:
                if p.name == "BDMV":
                    return p.parent
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
                        if keyword and re.search(r"%s" % keyword, str(event_path), re.IGNORECASE):
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
                if self.transferhis.get_by_src(str(event_path), storage=storage):
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
                downloader, download_hash = None, None
                if download_history:
                    downloader = download_history.downloader
                    download_hash = download_history.download_hash

                # 识别媒体信息
                if download_history and (download_history.tmdbid or download_history.doubanid):
                    # 下载记录中已存在识别信息
                    mediainfo: MediaInfo = self.mediaChain.recognize_media(mtype=MediaType(download_history.type),
                                                                           tmdbid=download_history.tmdbid,
                                                                           doubanid=download_history.doubanid)
                    if mediainfo:
                        # 更新自定义媒体类别
                        if download_history.media_category:
                            mediainfo.category = download_history.media_category

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
                dir_info = self.directoryhelper.get_dir(mediainfo, storage=storage, src_path=event_path)
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

                # 进入队列
                self.__transfer_queue(
                    TransferTask(
                        fileitem=file_item,
                        file_path=event_path,
                        meta=file_meta,
                        mediainfo=mediainfo,
                        target_directory=dir_info,
                        episodes_info=episodes_info,
                        downloader=downloader,
                        download_hash=download_hash
                    )
                )

            except Exception as e:
                logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def __collect_msg_medias(self, mediainfo: MediaInfo, file_meta: MetaInfoPath, transferinfo: TransferInfo):
        """
        收集媒体处理完的消息
        """
        media_list = self._msg_medias.get(mediainfo.title_year + " " + file_meta.season) or {}
        if media_list:
            media_files = media_list.get("files") or []
            if media_files:
                file_exists = False
                for file in media_files:
                    if str(transferinfo.fileitem.path) == file.get("path"):
                        file_exists = True
                        break
                if not file_exists:
                    media_files.append({
                        "path": str(transferinfo.fileitem.path),
                        "mediainfo": mediainfo,
                        "file_meta": file_meta,
                        "transferinfo": transferinfo
                    })
            else:
                media_files = [
                    {
                        "path": str(transferinfo.fileitem.path),
                        "mediainfo": mediainfo,
                        "file_meta": file_meta,
                        "transferinfo": transferinfo
                    }
                ]
            media_list = {
                "files": media_files,
                "time": datetime.datetime.now()
            }
        else:
            media_list = {
                "files": [
                    {
                        "path": str(transferinfo.fileitem.path),
                        "mediainfo": mediainfo,
                        "file_meta": file_meta,
                        "transferinfo": transferinfo
                    }
                ],
                "time": datetime.datetime.now()
            }
        self._msg_medias[mediainfo.title_year + " " + file_meta.season] = media_list

    def __send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if not self._msg_medias or not self._msg_medias.keys():
            return

        # 遍历检查是否已刮削完，发送消息
        for medis_title_year_season in list(self._msg_medias.keys()):
            media_list = self._msg_medias.get(medis_title_year_season)
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
            # 判断剧集最后更新时间距现在是已超过10秒或者电影，发送消息
            if (datetime.datetime.now() - last_update_time).total_seconds() > int(self._msg_interval) \
                    or mediainfo.type == MediaType.MOVIE:

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
                self.transferchain.send_transfer_message(meta=file_meta,
                                                         mediainfo=mediainfo,
                                                         transferinfo=transferinfo,
                                                         season_episode=season_episode)
                # 发送完消息，移出key
                del self._msg_medias[medis_title_year_season]
                continue

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
