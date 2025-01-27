import queue
import re
import threading
import traceback
from copy import deepcopy
from pathlib import Path
from queue import Queue
from time import sleep
from typing import List, Optional, Tuple, Union, Dict, Callable

from app import schemas
from app.chain import ChainBase
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings, global_vars
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfoPath
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.transferhistory import TransferHistory
from app.db.systemconfig_oper import SystemConfigOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.helper.format import FormatParser
from app.helper.progress import ProgressHelper
from app.log import logger
from app.schemas import TransferInfo, TransferTorrent, Notification, EpisodeFormat, FileItem, TransferDirectoryConf, \
    TransferTask, TransferQueue, TransferJob, TransferJobTask
from app.schemas.types import TorrentStatus, EventType, MediaType, ProgressKey, NotificationType, MessageChannel, \
    SystemConfigKey
from app.utils.singleton import Singleton
from app.utils.string import StringUtils

downloader_lock = threading.Lock()
job_lock = threading.Lock()
task_lock = threading.Lock()


class JobManager:
    """
    作业管理器
    """

    # 整理中的作业
    _job_view: Dict[Tuple, TransferJob] = {}
    # 汇总季集清单
    _season_episodes: Dict[Tuple, List[int]] = {}

    def __init__(self):
        self._job_view = {}
        self._season_episodes = {}

    @staticmethod
    def __get_meta_id(meta: MetaBase = None, season: int = None) -> Tuple:
        """
        获取元数据ID
        """
        return meta.name, season

    @staticmethod
    def __get_media_id(media: MediaInfo = None, season: int = None) -> Tuple:
        """
        获取媒体ID
        """
        if not media:
            return None, season
        return media.tmdb_id or media.douban_id, season

    def __get_id(self, task: TransferTask = None) -> Tuple:
        """
        获取作业ID
        """
        if task.mediainfo:
            return self.__get_media_id(media=task.mediainfo, season=task.meta.begin_season)
        else:
            return self.__get_meta_id(meta=task.meta, season=task.meta.begin_season)

    @staticmethod
    def __get_media(task: TransferTask) -> schemas.MediaInfo:
        """
        获取媒体信息
        """
        if task.mediainfo:
            # 有媒体信息
            mediainfo = deepcopy(task.mediainfo)
            mediainfo.clear()
            return schemas.MediaInfo(**mediainfo.to_dict())
        else:
            # 没有媒体信息
            meta: MetaBase = task.meta
            return schemas.MediaInfo(
                title=meta.name,
                year=meta.year,
                title_year=f"{meta.name} ({meta.year})",
                type=meta.type.value if meta.type else None
            )

    @staticmethod
    def __get_meta(task: TransferTask) -> schemas.MetaInfo:
        """
        获取元数据
        """
        return schemas.MetaInfo(**task.meta.to_dict())

    def add_task(self, task: TransferTask, state: str = "waiting"):
        """
        添加整理任务
        """
        if not any([task, task.meta, task.fileitem]):
            return
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                self._job_view[__mediaid__] = TransferJob(
                    media=self.__get_media(task),
                    season=task.meta.begin_season,
                    tasks=[TransferJobTask(
                        fileitem=task.fileitem,
                        meta=self.__get_meta(task),
                        downloader=task.downloader,
                        download_hash=task.download_hash,
                        state=state
                    )]
                )
            else:
                # 不重复添加任务
                if any([t.fileitem == task.fileitem for t in self._job_view[__mediaid__].tasks]):
                    return
                self._job_view[__mediaid__].tasks.append(
                    TransferJobTask(
                        fileitem=task.fileitem,
                        meta=self.__get_meta(task),
                        downloader=task.downloader,
                        download_hash=task.download_hash,
                        state=state
                    )
                )
            # 添加季集信息
            if self._season_episodes.get(__mediaid__):
                self._season_episodes[__mediaid__].extend(task.meta.episode_list)
                self._season_episodes[__mediaid__] = list(set(self._season_episodes[__mediaid__]))
            else:
                self._season_episodes[__mediaid__] = task.meta.episode_list

    def running_task(self, task: TransferTask):
        """
        任务运行中
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in self._job_view[__mediaid__].tasks:
                if t.fileitem == task.fileitem:
                    t.state = "running"
                    break

    def finish_task(self, task: TransferTask):
        """
        任务完成
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in self._job_view[__mediaid__].tasks:
                if t.fileitem == task.fileitem:
                    t.state = "completed"
                    break

    def fail_task(self, task: TransferTask):
        """
        任务失败
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in self._job_view[__mediaid__].tasks:
                if t.fileitem == task.fileitem:
                    t.state = "failed"
                    break
            # 移除剧集信息
            if __mediaid__ in self._season_episodes:
                self._season_episodes[__mediaid__] = list(
                    set(self._season_episodes[__mediaid__]) - set(task.meta.episode_list)
                )

    def remove_task(self, fileitem: FileItem) -> Optional[TransferJobTask]:
        """
        移除所有作业中的整理任务
        """
        with job_lock:
            for mediaid in list(self._job_view):
                job = self._job_view[mediaid]
                for task in job.tasks:
                    if task.fileitem == fileitem:
                        job.tasks.remove(task)
                        # 如果没有作业了，则移除作业
                        if not job.tasks:
                            self._job_view.pop(mediaid)
                        # 移除季集信息
                        if mediaid in self._season_episodes:
                            self._season_episodes[mediaid] = list(
                                set(self._season_episodes[mediaid]) - set(task.meta.episode_list)
                            )
                        return task

    def remove_job(self, task: TransferTask) -> Optional[TransferJob]:
        """
        移除作业
        """
        __mediaid__ = self.__get_media_id(media=task.mediainfo, season=task.meta.begin_season)
        with job_lock:
            # 移除作业
            if __mediaid__ in self._job_view:
                # 移除季集信息
                if __mediaid__ in self._season_episodes:
                    self._season_episodes.pop(__mediaid__)
                return self._job_view.pop(__mediaid__)

    def is_done(self, task: TransferTask) -> bool:
        """
        检查某项作业是否整理完成（不管成功还是失败）
        """
        __metaid__ = self.__get_meta_id(meta=task.meta, season=task.meta.begin_season)
        __mediaid__ = self.__get_media_id(media=task.mediainfo, season=task.meta.begin_season)
        if __metaid__ in self._job_view:
            meta_done = all(
                task.state in ["completed", "failed"] for task in self._job_view[__metaid__].tasks
            )
        else:
            meta_done = True
        if __mediaid__ != __metaid__:
            if __mediaid__ in self._job_view:
                media_done = all(
                    task.state in ["completed", "failed"] for task in self._job_view[__mediaid__].tasks
                )
            else:
                media_done = False
        else:
            media_done = True
        return meta_done and media_done

    def is_finished(self, task: TransferTask) -> bool:
        """
        检查某项作业是否已完成且有成功的记录
        """
        __metaid__ = self.__get_meta_id(meta=task.meta, season=task.meta.begin_season)
        __mediaid__ = self.__get_media_id(media=task.mediainfo, season=task.meta.begin_season)
        if __metaid__ in self._job_view:
            meta_finished = all(
                task.state in ["completed", "failed"] for task in self._job_view[__metaid__].tasks
            )
        else:
            meta_finished = True
        if __mediaid__ != __metaid__:
            if __mediaid__ in self._job_view:
                tasks = self._job_view[__mediaid__].tasks
                media_finished = all(
                    task.state in ["completed", "failed"] for task in tasks
                ) and any(
                    task.state == "completed" for task in tasks
                )
            else:
                media_finished = False
        else:
            media_finished = True
        return meta_finished and media_finished

    def is_success(self, task: TransferTask) -> bool:
        """
        检查某项作业是否全部成功
        """
        __metaid__ = self.__get_meta_id(meta=task.meta, season=task.meta.begin_season)
        __mediaid__ = self.__get_media_id(media=task.mediainfo, season=task.meta.begin_season)
        if __metaid__ in self._job_view:
            meta_success = all(
                task.state in ["completed"] for task in self._job_view[__metaid__].tasks
            )
        else:
            meta_success = True
        if __mediaid__ != __metaid__:
            if __mediaid__ in self._job_view:
                media_success = all(
                    task.state in ["completed"] for task in self._job_view[__mediaid__].tasks
                )
            else:
                media_success = False
        else:
            media_success = True
        return meta_success and media_success

    def success_tasks(self, media: MediaInfo, season: int = None) -> List[TransferJobTask]:
        """
        获取某项任务成功的任务
        """
        __mediaid__ = self.__get_media_id(media=media, season=season)
        with job_lock:
            if __mediaid__ not in self._job_view:
                return []
            return [task for task in self._job_view[__mediaid__].tasks if task.state == "completed"]

    def count(self, media: MediaInfo, season: int = None) -> int:
        """
        获取某项任务总数
        """
        __mediaid__ = self.__get_media_id(media=media, season=season)
        with job_lock:
            # 计算状态为完成的任务数
            if __mediaid__ not in self._job_view:
                return 0
            return len([task for task in self._job_view[__mediaid__].tasks if task.state == "completed"])

    def size(self, media: MediaInfo, season: int = None) -> int:
        """
        获取某项任务总大小
        """
        __mediaid__ = self.__get_media_id(media=media, season=season)
        with job_lock:
            # 计算状态为完成的任务数
            if __mediaid__ not in self._job_view:
                return 0
            return sum([task.fileitem.size for task in self._job_view[__mediaid__].tasks if task.state == "completed" and task.fileitem.size is not None])

    def total(self) -> int:
        """
        获取所有task任务总数
        """
        with job_lock:
            return sum([len(job.tasks) for job in self._job_view.values()])

    def list_jobs(self) -> List[TransferJob]:
        """
        获取任务列表
        """
        return list(self._job_view.values())

    def season_episodes(self, media: MediaInfo, season: int = None) -> List[int]:
        """
        获取季集清单
        """
        __mediaid__ = self.__get_media_id(media=media, season=season)
        with job_lock:
            return self._season_episodes.get(__mediaid__) or []


class TransferChain(ChainBase, metaclass=Singleton):
    """
    文件整理处理链
    """

    # 可处理的文件后缀
    all_exts = settings.RMT_MEDIAEXT

    # 待整理任务队列
    _queue = Queue()

    # 文件整理线程
    _transfer_thread = None

    # 队列间隔时间（秒）
    _transfer_interval = 15

    def __init__(self):
        super().__init__()
        self.downloadhis = DownloadHistoryOper()
        self.transferhis = TransferHistoryOper()
        self.progress = ProgressHelper()
        self.mediachain = MediaChain()
        self.tmdbchain = TmdbChain()
        self.storagechain = StorageChain()
        self.systemconfig = SystemConfigOper()
        self.directoryhelper = DirectoryHelper()
        self.jobview = JobManager()

        # 启动整理任务
        self.__init()

    def __init(self):
        """
        初始化
        """
        # 启动文件整理线程
        self._transfer_thread = threading.Thread(target=self.__start_transfer, daemon=True)
        self._transfer_thread.start()

    def __default_callback(self, task: TransferTask,
                           transferinfo: TransferInfo, /) -> Tuple[bool, str]:
        """
        整理完成后处理
        """
        if not transferinfo.success:
            # 转移失败
            logger.warn(f"{task.fileitem.name} 入库失败：{transferinfo.message}")
            # 新增转移失败历史记录
            self.transferhis.add_fail(
                fileitem=task.fileitem,
                mode=transferinfo.transfer_type if transferinfo else '',
                downloader=task.downloader,
                download_hash=task.download_hash,
                meta=task.meta,
                mediainfo=task.mediainfo,
                transferinfo=transferinfo
            )
            # 发送失败消息
            self.post_message(Notification(
                mtype=NotificationType.Manual,
                title=f"{task.mediainfo.title_year} {task.meta.season_episode} 入库失败！",
                text=f"原因：{transferinfo.message or '未知'}",
                image=task.mediainfo.get_message_image(),
                username=task.username,
                link=settings.MP_DOMAIN('#/history')
            ))
            # 整理失败
            self.jobview.fail_task(task)
            return False, transferinfo.message

        # 转移成功
        self.jobview.finish_task(task)
        logger.info(f"{task.fileitem.name} 入库成功：{transferinfo.target_diritem.path}")

        # 新增转移成功历史记录
        self.transferhis.add_success(
            fileitem=task.fileitem,
            mode=transferinfo.transfer_type if transferinfo else '',
            downloader=task.downloader,
            download_hash=task.download_hash,
            meta=task.meta,
            mediainfo=task.mediainfo,
            transferinfo=transferinfo
        )

        # 整理完成事件
        self.eventmanager.send_event(EventType.TransferComplete, {
            'fileitem': task.fileitem,
            'meta': task.meta,
            'mediainfo': task.mediainfo,
            'transferinfo': transferinfo,
            'downloader': task.downloader,
            'download_hash': task.download_hash,
        })

        with task_lock:
            # 全部整理成功时
            if self.jobview.is_success(task):
                # 移动模式删除空目录
                if transferinfo.transfer_type in ["move"]:
                    # 所有成功的业务
                    tasks = self.jobview.success_tasks(task.mediainfo, task.meta.begin_season)
                    # 记录已处理的种子hash
                    processed_hashes = set()
                    for t in tasks:
                        # 下载器hash
                        if t.download_hash and t.download_hash not in processed_hashes:
                            processed_hashes.add(t.download_hash)
                            if self.remove_torrents(t.download_hash, downloader=t.downloader):
                                logger.info(f"移动模式删除种子成功：{t.download_hash} ")
                        # 删除残留目录
                        if t.fileitem:
                            self.storagechain.delete_media_file(t.fileitem, delete_self=False)
            # 整理完成且有成功的任务时
            if self.jobview.is_finished(task):
                # 发送通知，实时手动整理时不发
                if transferinfo.need_notify and (task.background or not task.manual):
                    se_str = None
                    if task.mediainfo.type == MediaType.TV:
                        season_episodes = self.jobview.season_episodes(task.mediainfo, task.meta.begin_season)
                        if season_episodes:
                            se_str = f"{task.meta.season} {StringUtils.format_ep(season_episodes)}"
                        else:
                            se_str = f"{task.meta.season}"
                    # 更新文件数量
                    transferinfo.file_count = self.jobview.count(task.mediainfo, task.meta.begin_season) or 1
                    # 更新文件大小
                    transferinfo.total_size = self.jobview.size(task.mediainfo,
                                                                task.meta.begin_season) or task.fileitem.size
                    self.send_transfer_message(meta=task.meta,
                                               mediainfo=task.mediainfo,
                                               transferinfo=transferinfo,
                                               season_episode=se_str,
                                               username=task.username)
                # 刮削事件
                if transferinfo.need_scrape:
                    self.eventmanager.send_event(EventType.MetadataScrape, {
                        'meta': task.meta,
                        'mediainfo': task.mediainfo,
                        'fileitem': transferinfo.target_diritem
                    })

                # 移除已完成的任务
                self.jobview.remove_job(task)

        return True, ""

    def put_to_queue(self, task: TransferTask):
        """
        添加到待整理队列
        :param task: 任务信息
        """
        if not task:
            return
        # 维护整理任务视图
        self.__put_to_jobview(task)
        # 添加到队列
        self._queue.put(TransferQueue(
            task=task,
            callback=self.__default_callback
        ))

    def __put_to_jobview(self, task: TransferTask):
        """
        添加到作业视图
        """
        with task_lock:
            self.jobview.add_task(task)

    def remove_from_queue(self, fileitem: FileItem):
        """
        从待整理队列移除
        """
        if not fileitem:
            return
        self.jobview.remove_task(fileitem)

    def __start_transfer(self):
        """
        处理队列
        """
        # 队列开始标识
        __queue_start = True
        # 任务总数
        total_num = 0
        # 已处理总数
        processed_num = 0
        # 失败数量
        fail_num = 0

        while not global_vars.is_system_stopped:
            try:
                item: TransferQueue = self._queue.get(block=False)
                if item:
                    task = item.task
                    if not task:
                        continue
                    # 文件信息
                    fileitem = task.fileitem
                    # 开始新队列
                    if __queue_start:
                        logger.info("开始整理队列处理...")
                        # 启动进度
                        self.progress.start(ProgressKey.FileTransfer)
                        # 重置计数
                        processed_num = 0
                        fail_num = 0
                        total_num = self.jobview.total()
                        __process_msg = f"开始整理队列处理，当前共 {total_num} 个文件 ..."
                        logger.info(__process_msg)
                        self.progress.update(value=0,
                                             text=__process_msg,
                                             key=ProgressKey.FileTransfer)
                        # 队列已开始
                        __queue_start = False
                    # 更新进度
                    __process_msg = f"正在整理 {fileitem.name} ..."
                    logger.info(__process_msg)
                    self.progress.update(value=processed_num / total_num * 100,
                                         text=__process_msg,
                                         key=ProgressKey.FileTransfer)
                    # 整理
                    state, err_msg = self.__handle_transfer(task=task, callback=item.callback)
                    if not state:
                        # 任务失败
                        fail_num += 1
                    # 更新进度
                    processed_num += 1
                    __process_msg = f"{fileitem.name} 整理完成"
                    logger.info(__process_msg)
                    self.progress.update(value=processed_num / total_num * 100,
                                         text=__process_msg,
                                         key=ProgressKey.FileTransfer)
            except queue.Empty:
                if not __queue_start:
                    # 结束进度
                    __end_msg = f"整理队列处理完成，共整理 {processed_num} 个文件，失败 {fail_num} 个"
                    logger.info(__end_msg)
                    self.progress.update(value=100,
                                         text=__end_msg,
                                         key=ProgressKey.FileTransfer)
                    self.progress.end(ProgressKey.FileTransfer)
                    # 重置计数
                    processed_num = 0
                    fail_num = 0
                    # 标记为新队列
                    __queue_start = True

                # 等待一定时间，以让其他任务加入队列
                sleep(self._transfer_interval)
                continue
            except Exception as e:
                logger.error(f"整理队列处理出现错误：{e} - {traceback.format_exc()}")

    def __handle_transfer(self, task: TransferTask,
                          callback: Optional[Callable] = None) -> Tuple[bool, str]:
        """
        处理整理任务
        """
        try:
            # 识别
            if not task.mediainfo:
                mediainfo = None
                download_history = task.download_history
                # 下载用户
                if download_history:
                    task.username = download_history.username
                    # 识别媒体信息
                    if download_history.tmdbid or download_history.doubanid:
                        # 下载记录中已存在识别信息
                        mediainfo: Optional[MediaInfo] = self.recognize_media(mtype=MediaType(download_history.type),
                                                                              tmdbid=download_history.tmdbid,
                                                                              doubanid=download_history.doubanid)
                        if mediainfo:
                            # 更新自定义媒体类别
                            if download_history.media_category:
                                mediainfo.category = download_history.media_category
                else:
                    # 识别媒体信息
                    mediainfo = self.mediachain.recognize_by_meta(task.meta)

                # 更新媒体图片
                if mediainfo:
                    self.obtain_images(mediainfo=mediainfo)

                if not mediainfo:
                    # 新增整理失败历史记录
                    his = self.transferhis.add_fail(
                        fileitem=task.fileitem,
                        mode=task.transfer_type,
                        meta=task.meta,
                        downloader=task.downloader,
                        download_hash=task.download_hash
                    )
                    self.post_message(Notification(
                        mtype=NotificationType.Manual,
                        title=f"{task.fileitem.name} 未识别到媒体信息，无法入库！",
                        text=f"回复：```\n/redo {his.id} [tmdbid]|[类型]\n``` 手动识别整理。",
                        username=task.username,
                        link=settings.MP_DOMAIN('#/history')
                    ))
                    # 任务失败，直接移除task
                    self.jobview.remove_task(task.fileitem)
                    return False, "未识别到媒体信息"

                # 如果未开启新增已入库媒体是否跟随TMDB信息变化则根据tmdbid查询之前的title
                if not settings.SCRAP_FOLLOW_TMDB:
                    transfer_history = self.transferhis.get_by_type_tmdbid(tmdbid=mediainfo.tmdb_id,
                                                                           mtype=mediainfo.type.value)
                    if transfer_history:
                        mediainfo.title = transfer_history.title

                # 获取集数据
                if not task.episodes_info and mediainfo.type == MediaType.TV:
                    if task.meta.begin_season is None:
                        task.meta.begin_season = 1
                    mediainfo.season = mediainfo.season or task.meta.begin_season
                    task.episodes_info = self.tmdbchain.tmdb_episodes(
                        tmdbid=mediainfo.tmdb_id,
                        season=mediainfo.season
                    )

                # 更新任务信息
                task.mediainfo = mediainfo
                # 更新队列任务
                curr_task = self.jobview.remove_task(task.fileitem)
                self.jobview.add_task(task, state=curr_task.state if curr_task else "waiting")

            # 查询整理目标目录
            if not task.target_directory:
                if task.target_path:
                    # 指定目标路径，`手动整理`场景下使用，忽略源目录匹配，使用指定目录匹配
                    task.target_directory = self.directoryhelper.get_dir(media=task.mediainfo,
                                                                         dest_path=task.target_path,
                                                                         target_storage=task.target_storage)
                else:
                    # 启用源目录匹配时，根据源目录匹配下载目录，否则按源目录同盘优先原则，如无源目录，则根据媒体信息获取目标目录
                    task.target_directory = self.directoryhelper.get_dir(media=task.mediainfo,
                                                                         storage=task.fileitem.storage,
                                                                         src_path=Path(task.fileitem.path),
                                                                         target_storage=task.target_storage)

            # 正在处理
            self.jobview.running_task(task)

            # 执行整理
            transferinfo: TransferInfo = self.transfer(fileitem=task.fileitem,
                                                       meta=task.meta,
                                                       mediainfo=task.mediainfo,
                                                       target_directory=task.target_directory,
                                                       target_storage=task.target_storage,
                                                       target_path=task.target_path,
                                                       transfer_type=task.transfer_type,
                                                       episodes_info=task.episodes_info,
                                                       scrape=task.scrape,
                                                       library_type_folder=task.library_type_folder,
                                                       library_category_folder=task.library_category_folder)
            if not transferinfo:
                logger.error("文件整理模块运行失败")
                return False, "文件整理模块运行失败"

            # 回调，位置传参：任务、整理结果
            if callback:
                return callback(task, transferinfo)

            return transferinfo.success, transferinfo.message

        finally:
            # 移除已完成的任务
            with task_lock:
                if self.jobview.is_done(task):
                    self.jobview.remove_job(task)

    def get_queue_tasks(self) -> List[TransferJob]:
        """
        获取整理任务列表
        """
        return self.jobview.list_jobs()

    def recommend_name(self, meta: MetaBase, mediainfo: MediaInfo) -> Optional[str]:
        """
        获取重命名后的名称
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :return: 重命名后的名称（含目录）
        """
        return self.run_module("recommend_name", meta=meta, mediainfo=mediainfo)

    def process(self) -> bool:
        """
        获取下载器中的种子列表，并执行整理
        """

        # 全局锁，避免重复处理
        with downloader_lock:
            # 获取下载器监控目录
            download_dirs = self.directoryhelper.get_download_dirs()
            # 如果没有下载器监控的目录则不处理
            if not any(dir_info.monitor_type == "downloader" and dir_info.storage == "local"
                       for dir_info in download_dirs):
                return True
            logger.info("开始整理下载器中已经完成下载的文件 ...")
            # 从下载器获取种子列表
            torrents: Optional[List[TransferTorrent]] = self.list_torrents(status=TorrentStatus.TRANSFER)
            if not torrents:
                logger.info("没有已完成下载但未整理的任务")
                return False

            logger.info(f"获取到 {len(torrents)} 个已完成的下载任务")

            for torrent in torrents:
                if global_vars.is_system_stopped:
                    break
                # 文件路径
                file_path = torrent.path
                if not file_path.exists():
                    logger.warn(f"文件不存在：{file_path}")
                    continue
                # 检查是否为下载器监控目录中的文件
                is_downloader_monitor = False
                for dir_info in download_dirs:
                    if dir_info.monitor_type != "downloader":
                        continue
                    if not dir_info.download_path:
                        continue
                    if file_path.is_relative_to(Path(dir_info.download_path)):
                        is_downloader_monitor = True
                        break
                if not is_downloader_monitor:
                    logger.debug(f"文件 {file_path} 不在下载器监控目录中，不通过下载器进行整理")
                    continue
                # 查询下载记录识别情况
                downloadhis: DownloadHistory = self.downloadhis.get_by_hash(torrent.hash)
                if downloadhis:
                    # 类型
                    try:
                        mtype = MediaType(downloadhis.type)
                    except ValueError:
                        mtype = MediaType.TV
                    # 按TMDBID识别
                    mediainfo = self.recognize_media(mtype=mtype,
                                                     tmdbid=downloadhis.tmdbid,
                                                     doubanid=downloadhis.doubanid)
                    if mediainfo:
                        # 补充图片
                        self.obtain_images(mediainfo)
                        # 更新自定义媒体类别
                        if downloadhis.media_category:
                            mediainfo.category = downloadhis.media_category
                else:
                    # 非MoviePilot下载的任务，按文件识别
                    mediainfo = None

                # 执行实时整理，匹配源目录
                state, errmsg = self.do_transfer(
                    fileitem=FileItem(
                        storage="local",
                        path=str(file_path).replace("\\", "/"),
                        type="dir" if not file_path.is_file() else "file",
                        name=file_path.name,
                        size=file_path.stat().st_size,
                        extension=file_path.suffix.lstrip('.'),
                    ),
                    mediainfo=mediainfo,
                    downloader=torrent.downloader,
                    download_hash=torrent.hash,
                    background=False,
                )

                # 设置下载任务状态
                if state:
                    self.transfer_completed(hashs=torrent.hash)

            # 结束
            logger.info("所有下载器中下载完成的文件已整理完成")
            return True

    def __get_trans_fileitems(self, fileitem: FileItem) -> List[Tuple[FileItem, bool]]:
        """
        获取整理目录或文件列表
        :param fileitem: 文件项
        """

        def __is_bluray_dir(_fileitem: FileItem) -> bool:
            """
            判断是不是蓝光目录
            """
            subs = self.storagechain.list_files(_fileitem)
            if subs:
                for sub in subs:
                    if sub.type == "dir" and sub.name in ["BDMV", "CERTIFICATE"]:
                        return True
            return False

        def __is_bluray_sub(_path: str) -> bool:
            """
            判断是否蓝光原盘目录内的子目录或文件
            """
            return True if re.search(r"BDMV[/\\]STREAM", _path, re.IGNORECASE) else False

        def __get_bluray_dir(_storage: str, _path: Path) -> Optional[FileItem]:
            """
            获取蓝光原盘BDMV目录的上级目录
            """
            for p in _path.parents:
                if p.name == "BDMV":
                    return self.storagechain.get_file_item(storage=_storage, path=p.parent)
            return None

        if not self.storagechain.get_item(fileitem):
            logger.warn(f"目录或文件不存在：{fileitem.path}")
            return []

        # 蓝光原盘子目录或文件
        if __is_bluray_sub(fileitem.path):
            dir_item = __get_bluray_dir(fileitem.storage, Path(fileitem.path))
            if dir_item:
                return [(dir_item, True)]

        # 单文件
        if fileitem.type == "file":
            return [(fileitem, False)]

        # 蓝光原盘根目录
        if __is_bluray_dir(fileitem):
            return [(fileitem, True)]

        # 需要整理的文件项列表
        trans_items = []
        # 先检查当前目录的下级目录，以支持合集的情况
        for sub_dir in self.storagechain.list_files(fileitem):
            if sub_dir.type == "dir":
                if __is_bluray_dir(sub_dir):
                    trans_items.append((sub_dir, True))
                else:
                    trans_items.append((sub_dir, False))

        if not trans_items:
            # 没有有效子目录，直接整理当前目录
            trans_items.append((fileitem, False))
        else:
            # 有子目录时，把当前目录的文件添加到整理任务中
            sub_items = self.storagechain.list_files(fileitem)
            if sub_items:
                trans_items.extend([(f, False) for f in sub_items if f.type == "file"])

        return trans_items

    def do_transfer(self, fileitem: FileItem,
                    meta: MetaBase = None, mediainfo: MediaInfo = None,
                    target_directory: TransferDirectoryConf = None,
                    target_storage: str = None, target_path: Path = None,
                    transfer_type: str = None, scrape: bool = None,
                    library_type_folder: bool = None, library_category_folder: bool = None,
                    season: int = None, epformat: EpisodeFormat = None, min_filesize: int = 0,
                    downloader: str = None, download_hash: str = None,
                    force: bool = False, background: bool = True,
                    manual: bool = False) -> Tuple[bool, str]:
        """
        执行一个复杂目录的整理操作
        :param fileitem: 文件项
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param target_directory:  目标目录配置
        :param target_storage: 目标存储器
        :param target_path: 目标路径
        :param transfer_type: 整理类型
        :param scrape: 是否刮削元数据
        :param library_type_folder: 媒体库类型子目录
        :param library_category_folder: 媒体库类别子目录
        :param season: 季
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param downloader: 下载器
        :param download_hash: 下载记录hash
        :param force: 是否强制整理
        :param background: 是否后台运行
        :param manual: 是否手动整理
        返回：成功标识，错误信息
        """

        def __is_allow_extensions(_ext: str) -> bool:
            """
            判断是否允许的扩展名
            """
            return True if not self.all_exts or f".{_ext.lower()}" in self.all_exts else False

        def __is_allow_filesize(_size: int, _min_filesize: int) -> bool:
            """
            判断是否满足最小文件大小
            """
            return True if not _min_filesize or _size > _min_filesize * 1024 * 1024 else False

        # 是否全部成功
        all_success = True

        # 自定义格式
        formaterHandler = FormatParser(eformat=epformat.format,
                                       details=epformat.detail,
                                       part=epformat.part,
                                       offset=epformat.offset) if epformat else None

        # 整理屏蔽词
        transfer_exclude_words = self.systemconfig.get(SystemConfigKey.TransferExcludeWords)
        # 汇总错误信息
        err_msgs: List[str] = []
        # 待整理目录或文件项
        trans_items = self.__get_trans_fileitems(fileitem)
        # 待整理的文件列表
        file_items: List[Tuple[FileItem, bool]] = []

        if not trans_items:
            logger.warn(f"{fileitem.path} 没有找到可整理的媒体文件")
            return False, f"{fileitem.name} 没有找到可整理的媒体文件"

        # 转换为所有待处理的文件清单
        for trans_item, bluray_dir in trans_items:
            # 如果是目录且不是⼀蓝光原盘，获取所有文件并整理
            if trans_item.type == "dir" and not bluray_dir:
                # 遍历获取下载目录所有文件（递归）
                if files := self.storagechain.list_files(trans_item, recursion=True):
                    file_items.extend([(file, False) for file in files])
            else:
                file_items.append((trans_item, bluray_dir))

        # 有集自定义格式，过滤文件
        if formaterHandler:
            file_items = [f for f in file_items if formaterHandler.match(f[0].name)]

        # 过滤后缀和大小
        file_items = [f for f in file_items if f[1]  # 蓝光目录不过滤
                      or __is_allow_extensions(f[0].extension) and __is_allow_filesize(f[0].size, min_filesize)]
        if not file_items:
            logger.warn(f"{fileitem.path} 没有找到可整理的媒体文件")
            return False, f"{fileitem.name} 没有找到可整理的媒体文件"

        logger.info(f"正在计划整理 {len(file_items)} 个文件...")

        # 整理所有文件
        transfer_tasks: List[TransferTask] = []
        for file_item, bluray_dir in file_items:
            if global_vars.is_system_stopped:
                break
            file_path = Path(file_item.path)
            # 回收站及隐藏的文件不处理
            if file_item.path.find('/@Recycle/') != -1 \
                    or file_item.path.find('/#recycle/') != -1 \
                    or file_item.path.find('/.') != -1 \
                    or file_item.path.find('/@eaDir') != -1:
                logger.debug(f"{file_item.path} 是回收站或隐藏的文件")
                continue

            # 整理屏蔽词不处理
            is_blocked = False
            if transfer_exclude_words:
                for keyword in transfer_exclude_words:
                    if not keyword:
                        continue
                    if keyword and re.search(r"%s" % keyword, file_item.path, re.IGNORECASE):
                        logger.info(f"{file_item.path} 命中整理屏蔽词 {keyword}，不处理")
                        is_blocked = True
                        break
            if is_blocked:
                continue

            # 整理成功的不再处理
            if not force:
                transferd = self.transferhis.get_by_src(file_item.path, storage=file_item.storage)
                if transferd:
                    if not transferd.status:
                        all_success = False
                    logger.info(f"{file_item.path} 已整理过，如需重新处理，请删除整理记录。")
                    err_msgs.append(f"{file_item.name} 已整理过")
                    continue

            if not meta:
                # 文件元数据
                file_meta = MetaInfoPath(file_path)
            else:
                file_meta = meta

            # 合并季
            if season is not None:
                file_meta.begin_season = season

            if not file_meta:
                all_success = False
                logger.error(f"{file_path.name} 无法识别有效信息")
                err_msgs.append(f"{file_path.name} 无法识别有效信息")
                continue

            # 自定义识别
            if formaterHandler:
                # 开始集、结束集、PART
                begin_ep, end_ep, part = formaterHandler.split_episode(file_name=file_path.name, file_meta=file_meta)
                if begin_ep is not None:
                    file_meta.begin_episode = begin_ep
                    file_meta.part = part
                if end_ep is not None:
                    file_meta.end_episode = end_ep

            # 根据父路径获取下载历史
            download_history = None
            if bluray_dir:
                # 蓝光原盘，按目录名查询
                download_history = self.downloadhis.get_by_path(str(file_path))
            else:
                # 按文件全路径查询
                download_file = self.downloadhis.get_file_by_fullpath(str(file_path))
                if download_file:
                    download_history = self.downloadhis.get_by_hash(download_file.download_hash)

            # 获取下载Hash
            if download_history and (not downloader or not download_hash):
                downloader = download_history.downloader
                download_hash = download_history.download_hash

            # 后台整理
            transfer_task = TransferTask(
                fileitem=file_item,
                meta=file_meta,
                mediainfo=mediainfo,
                target_directory=target_directory,
                target_storage=target_storage,
                target_path=target_path,
                transfer_type=transfer_type,
                scrape=scrape,
                library_type_folder=library_type_folder,
                library_category_folder=library_category_folder,
                downloader=downloader,
                download_hash=download_hash,
                download_history=download_history,
                manual=manual,
                background=background
            )
            if background:
                self.put_to_queue(task=transfer_task)
                logger.info(f"{file_path.name} 已添加到整理队列")
            else:
                # 加入列表
                self.__put_to_jobview(transfer_task)
                transfer_tasks.append(transfer_task)

        # 实时整理
        if transfer_tasks:
            # 总数量
            total_num = len(transfer_tasks)
            # 已处理数量
            processed_num = 0
            # 失败数量
            fail_num = 0

            # 启动进度
            self.progress.start(ProgressKey.FileTransfer)
            __process_msg = f"开始整理，共 {total_num} 个文件 ..."
            logger.info(__process_msg)
            self.progress.update(value=0,
                                 text=__process_msg,
                                 key=ProgressKey.FileTransfer)

            for transfer_task in transfer_tasks:
                if global_vars.is_system_stopped:
                    break
                # 更新进度
                __process_msg = f"正在整理 （{processed_num + fail_num + 1}/{total_num}）{transfer_task.fileitem.name} ..."
                logger.info(__process_msg)
                self.progress.update(value=(processed_num + fail_num) / total_num * 100,
                                     text=__process_msg,
                                     key=ProgressKey.FileTransfer)
                state, err_msg = self.__handle_transfer(
                    task=transfer_task,
                    callback=self.__default_callback
                )
                if not state:
                    all_success = False
                    logger.warn(f"{transfer_task.fileitem.name} {err_msg}")
                    err_msgs.append(f"{transfer_task.fileitem.name} {err_msg}")
                    fail_num += 1
                else:
                    processed_num += 1

            # 整理结束
            __end_msg = f"整理队列处理完成，共整理 {total_num} 个文件，失败 {fail_num} 个"
            logger.info(__end_msg)
            self.progress.update(value=100,
                                 text=__end_msg,
                                 key=ProgressKey.FileTransfer)
            self.progress.end(ProgressKey.FileTransfer)

        return all_success, "，".join(err_msgs)

    def remote_transfer(self, arg_str: str, channel: MessageChannel,
                        userid: Union[str, int] = None, source: str = None):
        """
        远程重新整理，参数 历史记录ID TMDBID|类型
        """

        def args_error():
            self.post_message(Notification(channel=channel, source=source,
                                           title="请输入正确的命令格式：/redo [id] [tmdbid/豆瓣id]|[类型]，"
                                                 "[id]整理记录编号", userid=userid))

        if not arg_str:
            args_error()
            return
        arg_strs = str(arg_str).split()
        if len(arg_strs) != 2:
            args_error()
            return
        # 历史记录ID
        logid = arg_strs[0]
        if not logid.isdigit():
            args_error()
            return
        # TMDBID/豆瓣ID
        id_strs = arg_strs[1].split('|')
        media_id = id_strs[0]
        if not logid.isdigit():
            args_error()
            return
        # 类型
        type_str = id_strs[1] if len(id_strs) > 1 else None
        if not type_str or type_str not in [MediaType.MOVIE.value, MediaType.TV.value]:
            args_error()
            return
        state, errmsg = self.__re_transfer(logid=int(logid),
                                           mtype=MediaType(type_str),
                                           mediaid=media_id)
        if not state:
            self.post_message(Notification(channel=channel, title="手动整理失败", source=source,
                                           text=errmsg, userid=userid, link=settings.MP_DOMAIN('#/history')))
            return

    def __re_transfer(self, logid: int, mtype: MediaType = None,
                      mediaid: str = None) -> Tuple[bool, str]:
        """
        根据历史记录，重新识别整理，只支持简单条件
        :param logid: 历史记录ID
        :param mtype: 媒体类型
        :param mediaid: TMDB ID/豆瓣ID
        """
        # 查询历史记录
        history: TransferHistory = self.transferhis.get(logid)
        if not history:
            logger.error(f"整理记录不存在，ID：{logid}")
            return False, "整理记录不存在"
        # 按源目录路径重新整理
        src_path = Path(history.src)
        if not src_path.exists():
            return False, f"源目录不存在：{src_path}"
        # 查询媒体信息
        if mtype and mediaid:
            mediainfo = self.recognize_media(mtype=mtype, tmdbid=int(mediaid) if str(mediaid).isdigit() else None,
                                             doubanid=mediaid)
            if mediainfo:
                # 更新媒体图片
                self.obtain_images(mediainfo=mediainfo)
        else:
            mediainfo = self.mediachain.recognize_by_path(str(src_path))
        if not mediainfo:
            return False, f"未识别到媒体信息，类型：{mtype.value}，id：{mediaid}"
        # 重新执行整理
        logger.info(f"{src_path.name} 识别为：{mediainfo.title_year}")

        # 删除旧的已整理文件
        if history.dest_fileitem:
            # 解析目标文件对象
            dest_fileitem = FileItem(**history.dest_fileitem)
            self.storagechain.delete_file(dest_fileitem)

        # 强制整理
        if history.src_fileitem:
            state, errmsg = self.do_transfer(fileitem=FileItem(**history.src_fileitem),
                                             mediainfo=mediainfo,
                                             download_hash=history.download_hash,
                                             force=True,
                                             background=False,
                                             manual=True)
            if not state:
                return False, errmsg

        return True, ""

    def manual_transfer(self,
                        fileitem: FileItem,
                        target_storage: str = None,
                        target_path: Path = None,
                        tmdbid: int = None,
                        doubanid: str = None,
                        mtype: MediaType = None,
                        season: int = None,
                        transfer_type: str = None,
                        epformat: EpisodeFormat = None,
                        min_filesize: int = 0,
                        scrape: bool = None,
                        library_type_folder: bool = None,
                        library_category_folder: bool = None,
                        force: bool = False,
                        background: bool = False) -> Tuple[bool, Union[str, list]]:
        """
        手动整理，支持复杂条件，带进度显示
        :param fileitem: 文件项
        :param target_storage: 目标存储
        :param target_path: 目标路径
        :param tmdbid: TMDB ID
        :param doubanid: 豆瓣ID
        :param mtype: 媒体类型
        :param season: 季度
        :param transfer_type: 整理类型
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param scrape: 是否刮削元数据
        :param library_type_folder: 是否按类型建立目录
        :param library_category_folder: 是否按类别建立目录
        :param force: 是否强制整理
        :param background: 是否后台运行
        """
        logger.info(f"手动整理：{fileitem.path} ...")
        if tmdbid or doubanid:
            # 有输入TMDBID时单个识别
            # 识别媒体信息
            mediainfo: MediaInfo = self.mediachain.recognize_media(tmdbid=tmdbid, doubanid=doubanid, mtype=mtype)
            if not mediainfo:
                return False, f"媒体信息识别失败，tmdbid：{tmdbid}，doubanid：{doubanid}，type: {mtype.value}"
            else:
                # 更新媒体图片
                self.obtain_images(mediainfo=mediainfo)
            # 开始进度
            self.progress.start(ProgressKey.FileTransfer)
            self.progress.update(value=0,
                                 text=f"开始整理 {fileitem.path} ...",
                                 key=ProgressKey.FileTransfer)
            # 开始整理
            state, errmsg = self.do_transfer(
                fileitem=fileitem,
                target_storage=target_storage,
                target_path=target_path,
                mediainfo=mediainfo,
                transfer_type=transfer_type,
                season=season,
                epformat=epformat,
                min_filesize=min_filesize,
                scrape=scrape,
                library_type_folder=library_type_folder,
                library_category_folder=library_category_folder,
                force=force,
                background=background,
                manual=True
            )
            if not state:
                return False, errmsg

            self.progress.end(ProgressKey.FileTransfer)
            logger.info(f"{fileitem.path} 整理完成")
            return True, ""
        else:
            # 没有输入TMDBID时，按文件识别
            state, errmsg = self.do_transfer(fileitem=fileitem,
                                             target_storage=target_storage,
                                             target_path=target_path,
                                             transfer_type=transfer_type,
                                             season=season,
                                             epformat=epformat,
                                             min_filesize=min_filesize,
                                             scrape=scrape,
                                             library_type_folder=library_type_folder,
                                             library_category_folder=library_category_folder,
                                             force=force,
                                             background=background,
                                             manual=True)
            return state, errmsg

    def send_transfer_message(self, meta: MetaBase, mediainfo: MediaInfo,
                              transferinfo: TransferInfo, season_episode: str = None, username: str = None):
        """
        发送入库成功的消息
        """
        msg_title = f"{mediainfo.title_year} {meta.season_episode if not season_episode else season_episode} 已入库"
        if mediainfo.vote_average:
            msg_str = f"评分：{mediainfo.vote_average}，类型：{mediainfo.type.value}"
        else:
            msg_str = f"类型：{mediainfo.type.value}"
        if mediainfo.category:
            msg_str = f"{msg_str}，类别：{mediainfo.category}"
        if meta.resource_term:
            msg_str = f"{msg_str}，质量：{meta.resource_term}"
        msg_str = f"{msg_str}，共{transferinfo.file_count}个文件，" \
                  f"大小：{StringUtils.str_filesize(transferinfo.total_size)}"
        if transferinfo.message:
            msg_str = f"{msg_str}，以下文件处理失败：\n{transferinfo.message}"
        # 发送
        self.post_message(Notification(
            mtype=NotificationType.Organize,
            title=msg_title, text=msg_str, image=mediainfo.get_message_image(),
            username=username,
            link=settings.MP_DOMAIN('#/history')))
