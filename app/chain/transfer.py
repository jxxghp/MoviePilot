import asyncio
import queue
import re
import threading
import traceback
from copy import deepcopy
from pathlib import Path
from typing import List, Optional, Tuple, Union, Dict, Callable

from app import schemas
from app.agent import ReplyMode, prompt_manager, agent_manager
from app.chain import ChainBase
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings, global_vars
from app.core.context import MediaInfo
from app.core.event import eventmanager
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfoPath
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.downloadhistory import DownloadHistory, DownloadFiles
from app.db.models.transferhistory import TransferHistory
from app.db.systemconfig_oper import SystemConfigOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.helper.format import FormatParser
from app.helper.progress import ProgressHelper
from app.log import logger
from app.schemas import StorageOperSelectionEventData
from app.schemas import (
    TransferInfo,
    Notification,
    EpisodeFormat,
    FileItem,
    TransferDirectoryConf,
    TransferTask,
    TransferQueue,
    TransferJob,
    TransferJobTask,
)
from app.schemas.exception import OperationInterrupted
from app.schemas.types import (
    TorrentStatus,
    EventType,
    MediaType,
    ProgressKey,
    NotificationType,
    MessageChannel,
    SystemConfigKey,
    ChainEventType,
    ContentType,
)
from app.utils.mixins import ConfigReloadMixin
from app.utils.singleton import Singleton
from app.utils.string import StringUtils
from app.utils.system import SystemUtils

# 下载器锁
downloader_lock = threading.Lock()
# 作业锁
job_lock = threading.Lock()
# 任务锁
task_lock = threading.Lock()


class JobManager:
    """
    作业管理器
    task任务负责一个文件的整理，job作业负责一个媒体的整理
    """

    # 整理中的作业
    _job_view: Dict[Tuple, TransferJob] = {}
    # 汇总季集清单
    _season_episodes: Dict[Tuple, List[int]] = {}
    # 记录从 meta 作业迁移到 media 作业的关系，用于清理提前失败后残留的 media 作业
    _meta_to_media_ids: Dict[Tuple, set[Tuple]] = {}

    def __init__(self):
        self._job_view = {}
        self._season_episodes = {}
        self._meta_to_media_ids = {}

    @staticmethod
    def __get_meta_id(meta: MetaBase = None, season: Optional[int] = None) -> Tuple:
        """
        获取元数据ID
        """
        return meta.name, season

    @staticmethod
    def __get_media_id(media: MediaInfo = None, season: Optional[int] = None) -> Tuple:
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
            return self.__get_media_id(
                media=task.mediainfo, season=task.meta.begin_season
            )
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
                type=meta.type.value if meta.type else None,
            )

    @staticmethod
    def __get_meta(task: TransferTask) -> schemas.MetaInfo:
        """
        获取元数据
        """
        return schemas.MetaInfo(**task.meta.to_dict())

    def add_task(self, task: TransferTask, state: Optional[str] = "waiting") -> bool:
        """
        添加整理任务，自动分组到对应的作业中
        :return: True表示任务已添加，False表示任务无效或已存在（重复）
        """
        if not all([task, task.meta, task.fileitem]):
            return False
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                self._job_view[__mediaid__] = TransferJob(
                    media=self.__get_media(task),
                    season=task.meta.begin_season,
                    tasks=[
                        TransferJobTask(
                            fileitem=task.fileitem,
                            meta=self.__get_meta(task),
                            downloader=task.downloader,
                            download_hash=task.download_hash,
                            state=state,
                        )
                    ],
                )
            else:
                # 不重复添加任务
                if any(
                        [
                            t.fileitem == task.fileitem
                            for t in self._job_view[__mediaid__].tasks
                        ]
                ):
                    logger.debug(f"任务 {task.fileitem.name} 已存在，跳过重复添加")
                    return False
                self._job_view[__mediaid__].tasks.append(
                    TransferJobTask(
                        fileitem=task.fileitem,
                        meta=self.__get_meta(task),
                        downloader=task.downloader,
                        download_hash=task.download_hash,
                        state=state,
                    )
                )
            # 添加季集信息
            if self._season_episodes.get(__mediaid__):
                self._season_episodes[__mediaid__].extend(task.meta.episode_list)
                self._season_episodes[__mediaid__] = list(
                    set(self._season_episodes[__mediaid__])
                )
            else:
                self._season_episodes[__mediaid__] = task.meta.episode_list
            return True

    def migrate_task(self, task: TransferTask) -> bool:
        """
        将任务从 meta 作业迁移到 media 作业
        """
        curr_task, source_job_id = self.__remove_task_with_job_id(task.fileitem)
        if not self.add_task(task, state=curr_task.state if curr_task else "waiting"):
            return False
        if curr_task and task.mediainfo:
            metaid = self.__get_meta_id(
                meta=task.meta, season=task.meta.begin_season
            )
            mediaid = self.__get_id(task)
            if source_job_id == metaid and mediaid != metaid:
                with job_lock:
                    self._meta_to_media_ids.setdefault(metaid, set()).add(mediaid)
        return True

    def __is_job_done(self, job_id: Tuple) -> bool:
        """
        检查指定作业是否已完成
        """
        if job_id not in self._job_view:
            return True
        return all(
            task.state in ["completed", "failed"]
            for task in self._job_view[job_id].tasks
        )

    def __pop_job(self, job_id: Tuple):
        """
        移除指定作业和对应季集缓存
        """
        if job_id in self._season_episodes:
            self._season_episodes.pop(job_id)
        if job_id in self._job_view:
            self._job_view.pop(job_id)

    def running_task(self, task: TransferTask):
        """
        设置任务为运行中
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
        设置任务为完成/成功
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
        设置任务为失败
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
                    set(self._season_episodes[__mediaid__])
                    - set(task.meta.episode_list)
                )

    def fail_unfinished_task(self, task: TransferTask):
        """
        将指定任务视图中的非终态任务标记为失败
        """
        if not task or not task.fileitem:
            return
        with job_lock:
            for mediaid, job in self._job_view.items():
                for job_task in job.tasks:
                    if job_task.fileitem != task.fileitem:
                        continue
                    if job_task.state not in ["completed", "failed"]:
                        job_task.state = "failed"
                        if mediaid in self._season_episodes:
                            self._season_episodes[mediaid] = list(
                                set(self._season_episodes[mediaid])
                                - set(task.meta.episode_list)
                            )
                    return

    def remove_task(self, fileitem: FileItem) -> Optional[TransferJobTask]:
        """
        根据文件项移除任务
        """
        task, _ = self.__remove_task_with_job_id(fileitem)
        return task

    def __remove_task_with_job_id(
            self, fileitem: FileItem
    ) -> Tuple[Optional[TransferJobTask], Optional[Tuple]]:
        """
        根据文件项移除任务，并返回任务所在的作业ID
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
                                set(self._season_episodes[mediaid])
                                - set(task.meta.episode_list)
                            )
                        return task, mediaid
            return None, None

    def remove_job(self, task: TransferTask) -> Optional[TransferJob]:
        """
        移除任务对应的作业（强制，线程不安全）
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ in self._job_view:
                # 移除季集信息
                if __mediaid__ in self._season_episodes:
                    self._season_episodes.pop(__mediaid__)
                return self._job_view.pop(__mediaid__)
            return None

    def try_remove_job(self, task: TransferTask):
        """
        尝试移除任务对应的作业（严格检查未完成作业，线程安全）
        """
        with job_lock:
            __metaid__ = self.__get_meta_id(
                meta=task.meta, season=task.meta.begin_season
            )
            __mediaid__ = self.__get_media_id(
                media=task.mediainfo, season=task.meta.begin_season
            )

            related_media_ids = set(self._meta_to_media_ids.get(__metaid__, set()))
            if task.mediainfo:
                related_media_ids.add(__mediaid__)

            meta_done = self.__is_job_done(__metaid__)
            media_done = all(
                self.__is_job_done(mediaid) for mediaid in related_media_ids
            )

            if meta_done and media_done:
                remove_ids = {__metaid__, self.__get_id(task), *related_media_ids}
                for job_id in remove_ids:
                    self.__pop_job(job_id)
                self._meta_to_media_ids.pop(__metaid__, None)

    def is_done(self, task: TransferTask) -> bool:
        """
        检查任务对应的作业是否整理完成（不管成功还是失败）
        """
        with job_lock:
            __metaid__ = self.__get_meta_id(
                meta=task.meta, season=task.meta.begin_season
            )
            __mediaid__ = self.__get_media_id(
                media=task.mediainfo, season=task.meta.begin_season
            )
            if __metaid__ in self._job_view:
                meta_done = all(
                    task.state in ["completed", "failed"]
                    for task in self._job_view[__metaid__].tasks
                )
            else:
                meta_done = True
            if __mediaid__ in self._job_view:
                media_done = all(
                    task.state in ["completed", "failed"]
                    for task in self._job_view[__mediaid__].tasks
                )
            else:
                media_done = True
            return meta_done and media_done

    def is_finished(self, task: TransferTask) -> bool:
        """
        检查任务对应的作业是否已完成且有成功的记录
        """
        with job_lock:
            __metaid__ = self.__get_meta_id(
                meta=task.meta, season=task.meta.begin_season
            )
            __mediaid__ = self.__get_media_id(
                media=task.mediainfo, season=task.meta.begin_season
            )
            if __metaid__ in self._job_view:
                meta_finished = all(
                    task.state in ["completed", "failed"]
                    for task in self._job_view[__metaid__].tasks
                )
            else:
                meta_finished = True
            if __mediaid__ in self._job_view:
                tasks = self._job_view[__mediaid__].tasks
                media_finished = all(
                    task.state in ["completed", "failed"] for task in tasks
                ) and any(task.state == "completed" for task in tasks)
            else:
                media_finished = True
            return meta_finished and media_finished

    def is_success(self, task: TransferTask) -> bool:
        """
        检查任务对应的作业是否全部成功
        """
        with job_lock:
            __metaid__ = self.__get_meta_id(
                meta=task.meta, season=task.meta.begin_season
            )
            __mediaid__ = self.__get_media_id(
                media=task.mediainfo, season=task.meta.begin_season
            )
            if __metaid__ in self._job_view:
                meta_success = all(
                    task.state in ["completed"]
                    for task in self._job_view[__metaid__].tasks
                )
            else:
                meta_success = True
            if __mediaid__ in self._job_view:
                media_success = all(
                    task.state in ["completed"]
                    for task in self._job_view[__mediaid__].tasks
                )
            else:
                media_success = True
            return meta_success and media_success

    def get_all_torrent_hashes(self) -> set[str]:
        """
        获取所有种子的哈希值集合
        """
        with job_lock:
            return {
                task.download_hash
                for job in self._job_view.values()
                for task in job.tasks
            }

    def is_torrent_done(self, download_hash: str) -> bool:
        """
        检查指定种子的所有任务是否都已完成
        """
        with job_lock:
            if any(
                    task.state not in {"completed", "failed"}
                    for job in self._job_view.values()
                    for task in job.tasks
                    if task.download_hash == download_hash
            ):
                return False
            return True

    def is_torrent_success(self, download_hash: str) -> bool:
        """
        检查指定种子的所有任务是否都已成功
        """
        with job_lock:
            if any(
                    task.state != "completed"
                    for job in self._job_view.values()
                    for task in job.tasks
                    if task.download_hash == download_hash
            ):
                return False
            return True

    def has_tasks(
            self,
            meta: MetaBase,
            mediainfo: Optional[MediaInfo] = None,
            season: Optional[int] = None,
    ) -> bool:
        """
        判断作业是否还有任务正在处理
        """
        with job_lock:
            if mediainfo:
                __mediaid__ = self.__get_media_id(media=mediainfo, season=season)
                if __mediaid__ in self._job_view:
                    return True

            __metaid__ = self.__get_meta_id(meta=meta, season=season)
            return (
                    __metaid__ in self._job_view
                    and len(self._job_view[__metaid__].tasks) > 0
            )

    def success_tasks(
            self, media: MediaInfo, season: Optional[int] = None
    ) -> List[TransferJobTask]:
        """
        获取作业中所有成功的任务
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            if __mediaid__ not in self._job_view:
                return []
            return [
                task
                for task in self._job_view[__mediaid__].tasks
                if task.state == "completed"
            ]

    def all_tasks(
            self, media: MediaInfo, season: Optional[int] = None
    ) -> List[TransferJobTask]:
        """
        获取作业中全部任务
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            if __mediaid__ not in self._job_view:
                return []
            return self._job_view[__mediaid__].tasks

    def count(self, media: MediaInfo, season: Optional[int] = None) -> int:
        """
        获取作业中成功总数
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            if __mediaid__ not in self._job_view:
                return 0
            return len(
                [
                    task
                    for task in self._job_view[__mediaid__].tasks
                    if task.state == "completed"
                ]
            )

    def size(self, media: MediaInfo, season: Optional[int] = None) -> int:
        """
        获取作业中所有成功文件总大小
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            if __mediaid__ not in self._job_view:
                return 0
            return sum(
                [
                    task.fileitem.size
                    if task.fileitem.size is not None
                    else (
                        SystemUtils.get_directory_size(Path(task.fileitem.path))
                        if task.fileitem.storage == "local"
                        else 0
                    )
                    for task in self._job_view[__mediaid__].tasks
                    if task.state == "completed"
                ]
            )

    def total(self) -> int:
        """
        获取所有任务总数
        """
        with job_lock:
            return sum([len(job.tasks) for job in self._job_view.values()])

    def list_jobs(self) -> List[TransferJob]:
        """
        获取所有作业的任务列表
        """
        with job_lock:
            return list(self._job_view.values())

    def season_episodes(
            self, media: MediaInfo, season: Optional[int] = None
    ) -> List[int]:
        """
        获取作业的季集清单
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            return self._season_episodes.get(__mediaid__) or []


class FailedRetryScheduler:
    """
    负责失败整理记录的 debounce 聚合与 AI 重试调度。
    """

    RETRY_TRANSFER_DEBOUNCE_SECONDS = 300

    def __init__(self):
        super().__init__()
        self._retry_transfer_buffer: dict[str, list[int]] = {}
        self._retry_transfer_timers: dict[str, asyncio.TimerHandle] = {}
        self._retry_transfer_lock = asyncio.Lock()

    async def close(self):
        async with self._retry_transfer_lock:
            timers = list(self._retry_transfer_timers.values())
            self._retry_transfer_timers.clear()
            self._retry_transfer_buffer.clear()

        for timer in timers:
            timer.cancel()

    @staticmethod
    def _build_retry_transfer_template_context(
            history_ids: list[int],
    ) -> tuple[str, dict[str, int | str]]:
        """仅负责把失败重试任务的动态数据映射成模板变量。"""
        is_batch = len(history_ids) > 1
        task_type = "batch_transfer_failed_retry" if is_batch else "transfer_failed_retry"
        template_context: dict[str, int | str] = {
            "history_ids_csv": ", ".join(str(item) for item in history_ids),
            "history_count": len(history_ids),
        }
        if not is_batch:
            template_context["history_id"] = history_ids[0]
        return task_type, template_context

    def _build_retry_transfer_prompt(self, history_ids: list[int]) -> str:
        """根据失败记录数量构建统一的重试整理后台任务提示词。"""
        task_type, template_context = self._build_retry_transfer_template_context(history_ids)
        return prompt_manager.render_system_task_message(
            task_type,
            template_context=template_context,
        )

    async def schedule_retry(self, history_id: int, group_key: str = ""):
        """
        同一 group_key 的失败记录会在缓冲期内合并为一次 agent 调用。
        """
        if not group_key:
            group_key = f"_default_{history_id}"

        async with self._retry_transfer_lock:
            if group_key not in self._retry_transfer_buffer:
                self._retry_transfer_buffer[group_key] = []
            if history_id not in self._retry_transfer_buffer[group_key]:
                self._retry_transfer_buffer[group_key].append(history_id)
                logger.info(
                    f"智能体重试整理：记录 ID={history_id} 已加入缓冲区 "
                    f"(group={group_key}, 当前{len(self._retry_transfer_buffer[group_key])}条)"
                )

            if group_key in self._retry_transfer_timers:
                self._retry_transfer_timers[group_key].cancel()

            loop = asyncio.get_running_loop()
            self._retry_transfer_timers[group_key] = loop.call_later(
                self.RETRY_TRANSFER_DEBOUNCE_SECONDS,
                lambda gk=group_key: asyncio.create_task(self._flush_retry_transfer(gk)),
            )

    async def _flush_retry_transfer(self, group_key: str):
        """
        延迟定时器到期后，取出该分组的所有 history_id 并合并为一次 agent 调用。
        """
        async with self._retry_transfer_lock:
            history_ids = self._retry_transfer_buffer.pop(group_key, [])
            self._retry_transfer_timers.pop(group_key, None)

        if not history_ids:
            return

        ids_str = ", ".join(str(item) for item in history_ids)
        logger.info(
            f"智能体重试整理：开始批量处理失败记录 IDs=[{ids_str}] (group={group_key})"
        )

        try:
            await agent_manager.run_background_prompt(
                message=self._build_retry_transfer_prompt(history_ids),
                session_prefix="__agent_retry_transfer_batch",
                reply_mode=ReplyMode.DISPATCH,
            )
            logger.info(
                f"智能体重试整理：批量处理完成 IDs=[{ids_str}] (group={group_key})"
            )
        except Exception as err:
            logger.error(
                f"智能体重试整理失败 (IDs=[{ids_str}], group={group_key}): {err}"
            )


class TransferChain(ChainBase, ConfigReloadMixin, metaclass=Singleton):
    """
    文件整理处理链
    """

    CONFIG_WATCH = {
        "TRANSFER_THREADS",
    }

    def __init__(self):
        super().__init__()
        # 主要媒体文件后缀
        self._media_exts = settings.RMT_MEDIAEXT
        # 字幕文件后缀
        self._subtitle_exts = settings.RMT_SUBEXT
        # 音频文件后缀
        self._audio_exts = settings.RMT_AUDIOEXT
        # 可处理的文件后缀（视频文件、字幕、音频文件）
        self._allowed_exts = self._media_exts + self._audio_exts + self._subtitle_exts
        # 待整理任务队列
        self._queue = queue.Queue()
        # 文件整理线程
        self._transfer_threads = []
        # 队列间隔时间（秒）
        self._transfer_interval = 15
        # 事件管理器
        self.jobview = JobManager()
        # Agent重试管理器
        self.retry_scheduler = FailedRetryScheduler()
        # 转移成功的文件清单
        self._success_target_files: Dict[str, List[str]] = {}
        # 整理进度进度
        self._progress = ProgressHelper(ProgressKey.FileTransfer)
        # 队列相关状态
        self._threads = []
        self._queue_active = False
        self._active_tasks = 0
        self._processed_num = 0
        self._fail_num = 0
        self._total_num = 0
        # 启动整理任务
        self.__init()

    def __init(self):
        """
        启动文件整理线程
        """
        self._queue_active = True
        for i in range(settings.TRANSFER_THREADS):
            logger.info(f"启动文件整理线程 {i + 1} ...")
            thread = threading.Thread(
                target=self.__start_transfer, name=f"transfer-{i}", daemon=True
            )
            self._threads.append(thread)
            thread.start()

    def __stop(self):
        """
        停止文件整理进程
        """
        self._queue_active = False
        for thread in self._threads:
            thread.join()
        self._threads = []
        logger.info("文件整理线程已停止")

    def on_config_changed(self):
        self.__stop()
        self.__init()

    def __is_subtitle_file(self, fileitem: FileItem) -> bool:
        """
        判断是否为字幕文件
        """
        if not fileitem.extension:
            return False
        return (
            True if f".{fileitem.extension.lower()}" in self._subtitle_exts else False
        )

    def __is_audio_file(self, fileitem: FileItem) -> bool:
        """
        判断是否为音频文件
        """
        if not fileitem.extension:
            return False
        return True if f".{fileitem.extension.lower()}" in self._audio_exts else False

    def __is_media_file(self, fileitem: FileItem) -> bool:
        """
        判断是否为主要媒体文件
        """
        if fileitem.type == "dir":
            # 蓝光原盘判断
            return StorageChain().is_bluray_folder(fileitem)
        if not fileitem.extension:
            return False
        return True if f".{fileitem.extension.lower()}" in self._media_exts else False

    def __is_allowed_file(self, fileitem: FileItem) -> bool:
        """
        判断是否允许的扩展名
        """
        if not fileitem.extension:
            return False
        return True if f".{fileitem.extension.lower()}" in self._allowed_exts else False

    @staticmethod
    def __is_allow_filesize(fileitem: FileItem, min_filesize: int) -> bool:
        """
        判断是否满足最小文件大小
        """
        return (
            True
            if not min_filesize or (fileitem.size or 0) > min_filesize * 1024 * 1024
            else False
        )

    def __default_callback(
            self, task: TransferTask, transferinfo: TransferInfo, /
    ) -> Tuple[bool, str]:
        """
        整理完成后处理
        """

        # 状态
        ret_status = True
        # 错误信息
        ret_message = ""

        def __notify():
            """
            完成时发送消息、刮削事件、移除任务等
            """
            # 更新文件数量
            transferinfo.file_count = (
                    self.jobview.count(task.mediainfo, task.meta.begin_season) or 1
            )
            # 更新文件大小
            transferinfo.total_size = (
                    self.jobview.size(task.mediainfo, task.meta.begin_season)
                    or task.fileitem.size
            )
            # 更新文件清单
            with job_lock:
                transferinfo.file_list_new = self._success_target_files.pop(
                    transferinfo.target_diritem.path, []
                )

            # 发送通知，实时手动整理时不发
            if transferinfo.need_notify and (task.background or not task.manual):
                se_str = None
                if task.mediainfo.type == MediaType.TV:
                    season_episodes = self.jobview.season_episodes(
                        task.mediainfo, task.meta.begin_season
                    )
                    if season_episodes:
                        se_str = f"{task.meta.season} {StringUtils.format_ep(season_episodes)}"
                    else:
                        se_str = f"{task.meta.season}"
                # 发送入库成功消息
                self.send_transfer_message(
                    meta=task.meta,
                    mediainfo=task.mediainfo,
                    transferinfo=transferinfo,
                    season_episode=se_str,
                    username=task.username,
                )

            # 刮削事件
            if transferinfo.need_scrape and self.__is_media_file(task.fileitem):
                self.eventmanager.send_event(
                    EventType.MetadataScrape,
                    {
                        "meta": task.meta,
                        "mediainfo": task.mediainfo,
                        "fileitem": transferinfo.target_diritem,
                        "file_list": transferinfo.file_list_new,
                        "overwrite": False,
                    },
                )

        transferhis = TransferHistoryOper()

        # 转移失败
        if not transferinfo.success:
            logger.warn(f"{task.fileitem.name} 入库失败：{transferinfo.message}")

            # 新增转移失败历史记录
            history = transferhis.add_fail(
                fileitem=task.fileitem,
                mode=transferinfo.transfer_type if transferinfo else "",
                downloader=task.downloader,
                download_hash=task.download_hash,
                meta=task.meta,
                mediainfo=task.mediainfo,
                transferinfo=transferinfo,
            )

            # 整理失败事件
            if self.__is_media_file(task.fileitem):
                # 主要媒体文件整理失败事件
                self.eventmanager.send_event(
                    EventType.TransferFailed,
                    {
                        "fileitem": task.fileitem,
                        "meta": task.meta,
                        "mediainfo": task.mediainfo,
                        "transferinfo": transferinfo,
                        "downloader": task.downloader,
                        "download_hash": task.download_hash,
                        "transfer_history_id": history.id if history else None,
                    },
                )
            elif self.__is_subtitle_file(task.fileitem):
                # 字幕整理失败事件
                self.eventmanager.send_event(
                    EventType.SubtitleTransferFailed,
                    {
                        "fileitem": task.fileitem,
                        "meta": task.meta,
                        "mediainfo": task.mediainfo,
                        "transferinfo": transferinfo,
                        "downloader": task.downloader,
                        "download_hash": task.download_hash,
                        "transfer_history_id": history.id if history else None,
                    },
                )
            elif self.__is_audio_file(task.fileitem):
                # 音频文件整理失败事件
                self.eventmanager.send_event(
                    EventType.AudioTransferFailed,
                    {
                        "fileitem": task.fileitem,
                        "meta": task.meta,
                        "mediainfo": task.mediainfo,
                        "transferinfo": transferinfo,
                        "downloader": task.downloader,
                        "download_hash": task.download_hash,
                        "transfer_history_id": history.id if history else None,
                    },
                )

            # 发送失败消息
            self.post_message(
                Notification(
                    mtype=NotificationType.Manual,
                    title=f"{task.mediainfo.title_year} {task.meta.season_episode} 入库失败！",
                    text="\n".join(
                        [
                            f"原因：{transferinfo.message or '未知'}",
                            (
                                f"如果按钮不可用，可回复：\n```\n/redo {history.id}\n```"
                                if history
                                else ""
                            ),
                        ]
                    ).strip(),
                    image=task.mediainfo.get_message_image(),
                    username=task.username,
                    link=settings.MP_DOMAIN("#/history"),
                    buttons=self.build_failed_transfer_buttons(
                        history.id if history else None
                    ),
                )
            )

            # 设置任务失败
            self.jobview.fail_task(task)

            # AI智能体自动重试整理
            if (
                    history
                    and settings.AI_AGENT_ENABLE
                    and settings.AI_AGENT_RETRY_TRANSFER
            ):
                try:
                    # 使用 download_hash 或源文件父目录作为分组键，
                    # 同一批次（如同一个种子）的失败记录会被合并为一次agent调用
                    group_key = (
                        task.download_hash or str(task.fileitem.path).rsplit("/", 1)[0]
                        if task.fileitem
                        else ""
                    )
                    asyncio.run_coroutine_threadsafe(
                        self.retry_scheduler.schedule_retry(
                            history.id, group_key=group_key
                        ),
                        global_vars.loop,
                    )
                    logger.info(f"已触发AI智能体重试整理历史记录 #{history.id}")
                except Exception as e:
                    logger.error(f"触发AI智能体重试整理失败: {e}")

            # 返回失败
            ret_status = False
            ret_message = transferinfo.message

        else:
            # 转移成功
            logger.info(
                f"{task.fileitem.name} 入库成功：{transferinfo.target_diritem.path}"
            )

            # 新增task转移成功历史记录
            history = transferhis.add_success(
                fileitem=task.fileitem,
                mode=transferinfo.transfer_type if transferinfo else "",
                downloader=task.downloader,
                download_hash=task.download_hash,
                meta=task.meta,
                mediainfo=task.mediainfo,
                transferinfo=transferinfo,
            )

            # task整理完成事件
            if self.__is_media_file(task.fileitem):
                # 主要媒体文件整理完成事件
                self.eventmanager.send_event(
                    EventType.TransferComplete,
                    {
                        "fileitem": task.fileitem,
                        "meta": task.meta,
                        "mediainfo": task.mediainfo,
                        "transferinfo": transferinfo,
                        "downloader": task.downloader,
                        "download_hash": task.download_hash,
                        "transfer_history_id": history.id if history else None,
                    },
                )
            elif self.__is_subtitle_file(task.fileitem):
                # 字幕整理完成事件
                self.eventmanager.send_event(
                    EventType.SubtitleTransferComplete,
                    {
                        "fileitem": task.fileitem,
                        "meta": task.meta,
                        "mediainfo": task.mediainfo,
                        "transferinfo": transferinfo,
                        "downloader": task.downloader,
                        "download_hash": task.download_hash,
                        "transfer_history_id": history.id if history else None,
                    },
                )
            elif self.__is_audio_file(task.fileitem):
                # 音频文件整理完成事件
                self.eventmanager.send_event(
                    EventType.AudioTransferComplete,
                    {
                        "fileitem": task.fileitem,
                        "meta": task.meta,
                        "mediainfo": task.mediainfo,
                        "transferinfo": transferinfo,
                        "downloader": task.downloader,
                        "download_hash": task.download_hash,
                        "transfer_history_id": history.id if history else None,
                    },
                )

            # task登记转移成功文件清单
            target_dir_path = transferinfo.target_diritem.path
            target_files = transferinfo.file_list_new
            with job_lock:
                if self._success_target_files.get(target_dir_path):
                    self._success_target_files[target_dir_path].extend(target_files)
                else:
                    self._success_target_files[target_dir_path] = target_files

            # 设置任务成功
            self.jobview.finish_task(task)

        # 全部整理完成且有成功的任务时，发送消息和事件
        if self.jobview.is_finished(task):
            __notify()

        # 只要该种子的所有任务都已整理完成，则设置种子状态为已整理
        if task.download_hash and self.jobview.is_torrent_done(task.download_hash):
            self.transfer_completed(
                hashs=task.download_hash, downloader=task.downloader
            )

        # 移动模式，全部成功时删除空目录和种子文件
        if transferinfo.transfer_type in ["move"]:
            # 全部整理成功时
            if self.jobview.is_success(task):
                # 所有成功的业务
                tasks = self.jobview.success_tasks(
                    task.mediainfo, task.meta.begin_season
                )
                # 获取整理屏蔽词
                transfer_exclude_words = SystemConfigOper().get(
                    SystemConfigKey.TransferExcludeWords
                )
                processed_hashes = set()
                for t in tasks:
                    if t.download_hash and t.download_hash not in processed_hashes:
                        # 检查该种子的所有任务（跨作业）是否都已成功
                        if self.jobview.is_torrent_success(t.download_hash):
                            processed_hashes.add(t.download_hash)
                            if self._can_delete_torrent(
                                    t.download_hash, t.downloader, transfer_exclude_words
                            ):
                                # 移除种子及文件
                                if self.remove_torrents(
                                        t.download_hash, downloader=t.downloader
                                ):
                                    logger.info(
                                        f"移动模式删除种子成功：{t.download_hash}"
                                    )
                    if not t.download_hash and t.fileitem:
                        # 删除剩余空目录
                        StorageChain().delete_media_file(t.fileitem, delete_self=False)

        return ret_status, ret_message

    def put_to_queue(self, task: TransferTask) -> bool:
        """
        添加到待整理队列
        :param task: 任务信息
        :return: True表示任务已添加到队列，False表示任务无效或已存在（重复）
        """
        if not task:
            return False
        # 维护整理任务视图，如果任务已存在则不添加到队列
        if not self.__put_to_jobview(task):
            return False
        # 添加到队列
        self._queue.put(TransferQueue(task=task, callback=self.__default_callback))
        return True

    def __put_to_jobview(self, task: TransferTask) -> bool:
        """
        添加到作业视图
        :return: True表示任务已添加，False表示任务无效或已存在（重复）
        """
        return self.jobview.add_task(task)

    def remove_from_queue(self, fileitem: FileItem):
        """
        从待整理队列移除
        """
        if not fileitem:
            return
        self.jobview.remove_task(fileitem)

    def __fail_transfer_task(self, task: TransferTask):
        """
        标记异常整理任务失败并清理作业视图
        """
        self.jobview.fail_unfinished_task(task)
        if task.download_hash and self.jobview.is_torrent_done(task.download_hash):
            self.transfer_completed(
                hashs=task.download_hash, downloader=task.downloader
            )
        self.jobview.try_remove_job(task)

    def __start_transfer(self):
        """
        处理队列
        """
        while not global_vars.is_system_stopped and self._queue_active:
            try:
                item: TransferQueue = self._queue.get(
                    block=True, timeout=self._transfer_interval
                )
                if not item:
                    continue

                task = item.task
                if not task:
                    self._queue.task_done()
                    continue

                # 文件信息
                fileitem = task.fileitem

                with task_lock:
                    # 获取当前最新总数
                    current_total = self.jobview.total()
                    # 更新总数，取当前总数和当前已处理+运行中+队列中的最大值
                    self._total_num = max(self._total_num, current_total)

                    # 如果当前没有在运行的任务且处理数为0，说明是一个新序列的开始
                    if self._active_tasks == 0 and self._processed_num == 0:
                        logger.info("开始整理队列处理...")
                        # 启动进度
                        self._progress.start()
                        # 重置计数
                        self._processed_num = 0
                        self._fail_num = 0
                        __process_msg = (
                            f"开始整理队列处理，当前共 {self._total_num} 个文件 ..."
                        )
                        logger.info(__process_msg)
                        self._progress.update(value=0, text=__process_msg)
                    # 增加运行中的任务数
                    self._active_tasks += 1

                try:
                    # 更新进度
                    __process_msg = f"正在整理 {fileitem.name} ..."
                    logger.info(__process_msg)
                    with task_lock:
                        self._progress.update(
                            value=(self._processed_num / self._total_num * 100)
                            if self._total_num
                            else 0,
                            text=__process_msg,
                        )
                    # 整理
                    state, err_msg = self.__handle_transfer(
                        task=task, callback=item.callback
                    )

                    with task_lock:
                        if not state:
                            # 任务失败
                            self._fail_num += 1
                        # 更新进度
                        self._processed_num += 1
                        __process_msg = f"{fileitem.name} 整理完成"
                        logger.info(__process_msg)
                        self._progress.update(
                            value=(self._processed_num / self._total_num * 100)
                            if self._total_num
                            else 100,
                            text=__process_msg,
                        )
                except Exception as e:
                    logger.error(
                        f"{fileitem.name} 整理任务处理出现错误：{e} - {traceback.format_exc()}"
                    )
                    self.__fail_transfer_task(task)
                    with task_lock:
                        self._processed_num += 1
                        self._fail_num += 1
                finally:
                    self._queue.task_done()
                    with task_lock:
                        # 减少运行中的任务数
                        self._active_tasks -= 1
                        # 检查是否所有任务都已完成且队列为空
                        if self._active_tasks == 0 and self._queue.empty():
                            # 结束进度
                            __end_msg = f"整理队列处理完成，共整理 {self._processed_num} 个文件，失败 {self._fail_num} 个"
                            logger.info(__end_msg)
                            self._progress.update(value=100, text=__end_msg)
                            self._progress.end()
                            # 重置计数
                            self._processed_num = 0
                            self._fail_num = 0

            except queue.Empty:
                # 即使队列空了，如果还有任务在运行，也不应该结束进度
                # 这部分逻辑已经在 finally 的 active_tasks == 0 中处理了
                continue
            except Exception as e:
                logger.error(f"整理队列处理出现错误：{e} - {traceback.format_exc()}")

    def __handle_transfer(
            self, task: TransferTask, callback: Optional[Callable] = None
    ) -> Optional[Tuple[bool, str]]:
        """
        处理整理任务
        """
        try:
            # 识别
            transferhis = TransferHistoryOper()
            mediainfo = task.mediainfo
            mediainfo_changed = False
            if not mediainfo:
                download_history = task.download_history
                # 下载用户
                if download_history:
                    task.username = download_history.username
                    # 识别媒体信息
                    if download_history.tmdbid or download_history.doubanid:
                        # 下载记录中已存在识别信息
                        mediainfo: Optional[MediaInfo] = self.recognize_media(
                            mtype=MediaType(download_history.type),
                            tmdbid=download_history.tmdbid,
                            doubanid=download_history.doubanid,
                            episode_group=download_history.episode_group,
                        )
                        if mediainfo:
                            # 更新自定义媒体类别
                            if download_history.media_category:
                                mediainfo.category = download_history.media_category
                else:
                    # 识别媒体信息
                    mediainfo = MediaChain().recognize_by_meta(task.meta)

                # 更新媒体图片
                if mediainfo:
                    self.obtain_images(mediainfo=mediainfo)

                if not mediainfo:
                    # 新增整理失败历史记录
                    his = transferhis.add_fail(
                        fileitem=task.fileitem,
                        mode=task.transfer_type,
                        meta=task.meta,
                        downloader=task.downloader,
                        download_hash=task.download_hash,
                    )
                    self.post_message(
                        Notification(
                            mtype=NotificationType.Manual,
                            title=f"{task.fileitem.name} 未识别到媒体信息，无法入库！",
                            text=(
                                "原因：未识别到媒体信息\n"
                                "如果按钮不可用，可回复：\n"
                                f"```\n/redo {his.id}\n/redo {his.id} [tmdbid]|[类型]\n```\n"
                                "自动重试或手动识别整理。"
                            ),
                            username=task.username,
                            link=settings.MP_DOMAIN("#/history"),
                            buttons=self.build_failed_transfer_buttons(
                                his.id if his else None
                            ),
                        )
                    )
                    # 任务失败，直接移除task
                    self.jobview.remove_task(task.fileitem)

                    # AI智能体自动重试整理
                    if (
                            his
                            and settings.AI_AGENT_ENABLE
                            and settings.AI_AGENT_RETRY_TRANSFER
                    ):
                        try:
                            # 使用 download_hash 或源文件父目录作为分组键
                            group_key = (
                                task.download_hash
                                or str(task.fileitem.path).rsplit("/", 1)[0]
                                if task.fileitem
                                else ""
                            )
                            asyncio.run_coroutine_threadsafe(
                                self.retry_scheduler.schedule_retry(
                                    his.id, group_key=group_key
                                ),
                                global_vars.loop,
                            )
                            logger.info(f"已触发AI智能体重试整理历史记录 #{his.id}")
                        except Exception as e:
                            logger.error(f"触发AI智能体重试整理失败: {e}")

                    return False, "未识别到媒体信息"

                mediainfo_changed = True

            # 如果未开启新增已入库媒体是否跟随TMDB信息变化则根据tmdbid查询之前的title
            if not settings.SCRAP_FOLLOW_TMDB:
                transfer_history = transferhis.get_by_type_tmdbid(
                    tmdbid=mediainfo.tmdb_id, mtype=mediainfo.type.value
                )
                if transfer_history and mediainfo.title != transfer_history.title:
                    mediainfo.title = transfer_history.title
                    mediainfo_changed = True

            if mediainfo_changed:
                # 更新任务信息
                task.mediainfo = mediainfo
                # 更新队列任务
                self.jobview.migrate_task(task)

            # 获取集数据
            if task.mediainfo.type == MediaType.TV and not task.episodes_info:
                # 判断注意season为0的情况
                season_num = task.mediainfo.season
                if season_num is None and task.meta.season_seq:
                    if task.meta.season_seq.isdigit():
                        season_num = int(task.meta.season_seq)
                # 默认值1
                if season_num is None:
                    season_num = 1
                task.episodes_info = TmdbChain().tmdb_episodes(
                    tmdbid=task.mediainfo.tmdb_id,
                    season=season_num,
                    episode_group=task.mediainfo.episode_group,
                )

            # 查询整理目标目录
            if not task.target_directory:
                if task.target_path:
                    # 指定目标路径，`手动整理`场景下使用，忽略源目录匹配，使用指定目录匹配
                    task.target_directory = DirectoryHelper().get_dir(
                        media=task.mediainfo,
                        dest_path=task.target_path,
                        target_storage=task.target_storage,
                    )
                else:
                    # 启用源目录匹配时，根据源目录匹配下载目录，否则按源目录同盘优先原则，如无源目录，则根据媒体信息获取目标目录
                    task.target_directory = DirectoryHelper().get_dir(
                        media=task.mediainfo,
                        storage=task.fileitem.storage,
                        src_path=Path(task.fileitem.path),
                        target_storage=task.target_storage,
                    )
            if not task.target_storage and task.target_directory:
                task.target_storage = task.target_directory.library_storage

            # 正在处理
            self.jobview.running_task(task)

            # 广播事件，请示额外的源存储支持
            source_oper = None
            source_event_data = StorageOperSelectionEventData(
                storage=task.fileitem.storage,
            )
            source_event = eventmanager.send_event(
                ChainEventType.StorageOperSelection, source_event_data
            )
            # 使用事件返回的上下文数据
            if source_event and source_event.event_data:
                source_event_data: StorageOperSelectionEventData = (
                    source_event.event_data
                )
                if source_event_data.storage_oper:
                    source_oper = source_event_data.storage_oper

            # 广播事件，请示额外的目标存储支持
            target_oper = None
            target_event_data = StorageOperSelectionEventData(
                storage=task.target_storage,
            )
            target_event = eventmanager.send_event(
                ChainEventType.StorageOperSelection, target_event_data
            )
            # 使用事件返回的上下文数据
            if target_event and target_event.event_data:
                target_event_data: StorageOperSelectionEventData = (
                    target_event.event_data
                )
                if target_event_data.storage_oper:
                    target_oper = target_event_data.storage_oper

            # 执行整理
            transferinfo: TransferInfo = self.transfer(
                fileitem=task.fileitem,
                meta=task.meta,
                mediainfo=task.mediainfo,
                target_directory=task.target_directory,
                target_storage=task.target_storage,
                target_path=task.target_path,
                transfer_type=task.transfer_type,
                episodes_info=task.episodes_info,
                scrape=task.scrape,
                library_type_folder=task.library_type_folder,
                library_category_folder=task.library_category_folder,
                source_oper=source_oper,
                target_oper=target_oper,
            )
            if not transferinfo:
                logger.error("文件整理模块运行失败")
                return False, "文件整理模块运行失败"

            # 回调，位置传参：任务、整理结果
            if callback:
                return callback(task, transferinfo)

            return transferinfo.success, transferinfo.message

        finally:
            # 移除已完成的任务
            self.jobview.try_remove_job(task)

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
        # 全局锁，避免定时服务重复
        with downloader_lock:
            # 获取下载器监控目录
            download_dirs = DirectoryHelper().get_download_dirs()

            # 如果没有下载器监控的目录则不处理
            if not any(
                    dir_info.monitor_type == "downloader" and dir_info.storage == "local"
                    for dir_info in download_dirs
            ):
                return True

            logger.info("开始整理下载器中已经完成下载的文件 ...")

            # 从下载器获取种子列表
            if torrents_list := self.list_torrents(status=TorrentStatus.TRANSFER):
                seen = set()
                existing_hashes = self.jobview.get_all_torrent_hashes()
                torrents = [
                    torrent
                    for torrent in torrents_list
                    if (h := torrent.hash) not in existing_hashes
                       # 排除多下载器返回的重复种子
                       and (h not in seen and (seen.add(h) or True))
                ]
            else:
                torrents = []

            if not torrents:
                logger.info("没有已完成下载但未整理的任务")
                return False

            logger.info(f"获取到 {len(torrents)} 个已完成的下载任务")

            try:
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
                        logger.debug(
                            f"文件 {file_path} 不在下载器监控目录中，不通过下载器进行整理"
                        )
                        continue

                    # 查询下载记录识别情况
                    downloadhis: DownloadHistory = DownloadHistoryOper().get_by_hash(
                        torrent.hash
                    )
                    if downloadhis:
                        # 类型
                        try:
                            mtype = MediaType(downloadhis.type)
                        except ValueError:
                            mtype = MediaType.TV
                        # 识别媒体信息
                        mediainfo = self.recognize_media(
                            mtype=mtype,
                            tmdbid=downloadhis.tmdbid,
                            doubanid=downloadhis.doubanid,
                            episode_group=downloadhis.episode_group,
                        )
                        if mediainfo:
                            # 补充图片
                            self.obtain_images(mediainfo)
                            # 更新自定义媒体类别
                            if downloadhis.media_category:
                                mediainfo.category = downloadhis.media_category

                    else:
                        # 非MoviePilot下载的任务，按文件识别
                        mediainfo = None

                    # 执行异步整理，匹配源目录
                    self.do_transfer(
                        fileitem=FileItem(
                            storage="local",
                            path=file_path.as_posix()
                                 + ("/" if file_path.is_dir() else ""),
                            type="dir" if not file_path.is_file() else "file",
                            name=file_path.name,
                            size=file_path.stat().st_size,
                            extension=file_path.suffix.lstrip("."),
                        ),
                        mediainfo=mediainfo,
                        downloader=torrent.downloader,
                        download_hash=torrent.hash,
                    )

            finally:
                torrents.clear()
                del torrents

            return True

    def __get_trans_fileitems(
            self,
            fileitem: FileItem,
            predicate: Optional[Callable[[FileItem, bool], bool]],
            verify_file_exists: bool = True,
    ) -> List[Tuple[FileItem, bool]]:
        """
        获取待整理文件项列表

        :param fileitem: 源文件项
        :param predicate: 用于筛选目录或文件项
            该函数接收两个参数：

            - `file_item`: 需要判断的文件项（类型为 `FileItem`）
            - `is_bluray_dir`: 表示该项是否为蓝光原盘目录（布尔值）

            函数应返回 `True` 表示保留该项，`False` 表示过滤掉

            若 `predicate` 为 `None`，则默认保留所有项
        :param verify_file_exists: 验证目录或文件是否存在，默认值为 `True`
        """
        if global_vars.is_system_stopped:
            raise OperationInterrupted()

        storagechain = StorageChain()

        def __is_bluray_sub(_path: str) -> bool:
            """
            判断是否蓝光原盘目录内的子目录或文件
            """
            return (
                True if re.search(r"BDMV[/\\]STREAM", _path, re.IGNORECASE) else False
            )

        def __get_bluray_dir(_storage: str, _path: Path) -> Optional[FileItem]:
            """
            获取蓝光原盘BDMV目录的上级目录
            """
            for p in _path.parents:
                if p.name == "BDMV":
                    return storagechain.get_file_item(storage=_storage, path=p.parent)
            return None

        def _apply_predicate(
                file_item: FileItem, is_bluray_dir: bool
        ) -> List[Tuple[FileItem, bool]]:
            if predicate is None or predicate(file_item, is_bluray_dir):
                return [(file_item, is_bluray_dir)]
            return []

        if verify_file_exists:
            latest_fileitem = storagechain.get_item(fileitem)
            if not latest_fileitem:
                logger.warn(f"目录或文件不存在：{fileitem.path}")
                return []
            # 确保从历史记录重新整理时 能获得最新的源文件大小、修改日期等
            fileitem = latest_fileitem

        # 是否蓝光原盘子目录或文件
        if __is_bluray_sub(fileitem.path):
            if bluray_dir := __get_bluray_dir(fileitem.storage, Path(fileitem.path)):
                # 返回该文件所在的原盘根目录
                return _apply_predicate(bluray_dir, True)

        # 单文件
        if fileitem.type == "file":
            return _apply_predicate(fileitem, False)

        # 是否蓝光原盘根目录
        sub_items = storagechain.list_files(fileitem, recursion=False) or []
        if storagechain.contains_bluray_subdirectories(sub_items):
            # 当前目录是原盘根目录，不需要递归
            return _apply_predicate(fileitem, True)

        # 不是原盘根目录 递归获取目录内需要整理的文件项列表
        return [
            item
            for sub_item in sub_items
            for item in (
                self.__get_trans_fileitems(
                    sub_item, predicate, verify_file_exists=False
                )
                if sub_item.type == "dir"
                else _apply_predicate(sub_item, False)
            )
        ]

    @staticmethod
    def _get_shared_download_roots(file_path: Path) -> set[str]:
        """
        获取当前文件所在的共享下载根目录边界。

        父目录兜底回查只应在种子自身目录内进行，不能越过共享下载根目录，
        否则历史中的单文件/无子目录任务会污染同级其它文件的识别结果。
        """
        shared_roots: set[str] = set()
        media_type_dirs = {mtype.value for mtype in MediaType}

        for dir_info in DirectoryHelper().get_download_dirs():
            if not dir_info.download_path:
                continue

            download_root = Path(dir_info.download_path)
            if not file_path.is_relative_to(download_root):
                continue

            shared_roots.add(download_root.as_posix())
            relative_parts = file_path.relative_to(download_root).parts
            current_root = download_root
            part_index = 0

            if (
                    not dir_info.media_type
                    and dir_info.download_type_folder
                    and len(relative_parts) > part_index
                    and relative_parts[part_index] in media_type_dirs
            ):
                current_root = current_root / relative_parts[part_index]
                shared_roots.add(current_root.as_posix())
                part_index += 1

            if (
                    not dir_info.media_category
                    and dir_info.download_category_folder
                    and len(relative_parts) > part_index
            ):
                current_root = current_root / relative_parts[part_index]
                shared_roots.add(current_root.as_posix())

        return shared_roots

    @staticmethod
    def _match_download_file(
            download_file: DownloadFiles,
            file_path: Path,
            save_path: Path,
    ) -> bool:
        """
        判断下载文件记录是否明确对应当前文件。
        """
        if download_file.fullpath == file_path.as_posix():
            return True

        filepath = download_file.filepath
        if not filepath:
            return False

        try:
            return (save_path / Path(filepath)).as_posix() == file_path.as_posix()
        except (TypeError, ValueError):
            return False

    def _resolve_history_from_download_files(
            self,
            downloadhis: DownloadHistoryOper,
            download_files: List[DownloadFiles],
            file_path: Optional[Path] = None,
            save_path: Optional[Path] = None,
    ) -> Optional[DownloadHistory]:
        """
        从下载文件记录中解析唯一的下载历史。
        """
        if file_path and save_path:
            download_files = [
                download_file
                for download_file in download_files
                if self._match_download_file(
                    download_file=download_file,
                    file_path=file_path,
                    save_path=save_path,
                )
            ]

        download_hashes = {
            download_file.download_hash
            for download_file in download_files
            if download_file.download_hash
        }
        if len(download_hashes) == 1:
            return downloadhis.get_by_hash(next(iter(download_hashes)))
        return None

    def _resolve_download_history(
            self,
            downloadhis: DownloadHistoryOper,
            file_path: Path,
            bluray_dir: bool = False,
            download_hash: Optional[str] = None,
    ) -> Optional[DownloadHistory]:
        """
        根据显式 hash、文件路径或种子根目录回查下载历史。
        """
        if download_hash:
            return downloadhis.get_by_hash(download_hash)

        if bluray_dir:
            return downloadhis.get_by_path(file_path.as_posix())

        download_file = downloadhis.get_file_by_fullpath(file_path.as_posix())
        if download_file:
            return downloadhis.get_by_hash(download_file.download_hash)

        # 多文件种子里的字幕/附加文件可能没有稳定的 fullpath 记录，
        # 退回到父目录和 savepath 继续查找，尽量补齐同一种子的关联信息。
        shared_download_roots = self._get_shared_download_roots(file_path)

        for parent_path in file_path.parents:
            parent_posix = parent_path.as_posix()
            download_files = downloadhis.get_files_by_savepath(parent_posix) or []

            if parent_posix in shared_download_roots:
                # 共享下载根目录只能接受有明确文件记录的匹配，
                # 避免单文件/磁力任务把整个根目录污染成同一媒体。
                history = self._resolve_history_from_download_files(
                    downloadhis=downloadhis,
                    download_files=download_files,
                    file_path=file_path,
                    save_path=parent_path,
                )
                if history:
                    return history
                break

            download_history = downloadhis.get_by_path(parent_posix)
            if download_history:
                return download_history

            history = self._resolve_history_from_download_files(
                downloadhis=downloadhis,
                download_files=download_files,
            )
            if history:
                return history

        return None

    def do_transfer(
            self,
            fileitem: FileItem,
            meta: MetaBase = None,
            mediainfo: MediaInfo = None,
            target_directory: TransferDirectoryConf = None,
            target_storage: Optional[str] = None,
            target_path: Path = None,
            transfer_type: Optional[str] = None,
            scrape: Optional[bool] = None,
            library_type_folder: Optional[bool] = None,
            library_category_folder: Optional[bool] = None,
            season: Optional[int] = None,
            epformat: EpisodeFormat = None,
            min_filesize: Optional[int] = 0,
            downloader: Optional[str] = None,
            download_hash: Optional[str] = None,
            force: Optional[bool] = False,
            background: Optional[bool] = True,
            manual: Optional[bool] = False,
            continue_callback: Callable = None,
    ) -> Tuple[bool, str]:
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
        :param continue_callback: 继续处理回调
        返回：成功标识，错误信息
        """
        # 是否全部成功
        all_success = True

        # 自定义格式
        formaterHandler = (
            FormatParser(
                eformat=epformat.format,
                details=epformat.detail,
                part=epformat.part,
                offset=epformat.offset,
            )
            if epformat
            else None
        )

        # 整理屏蔽词
        transfer_exclude_words = SystemConfigOper().get(
            SystemConfigKey.TransferExcludeWords
        )
        # 汇总错误信息
        err_msgs: List[str] = []

        def _filter(item: FileItem, is_bluray_dir: bool) -> bool:
            """
            过滤文件项

            :return: True 表示保留，False 表示排除
            """
            if continue_callback and not continue_callback():
                raise OperationInterrupted()
            # 有集自定义格式，过滤文件
            if formaterHandler and not formaterHandler.match(item.name):
                return False
            # 过滤后缀和大小（蓝光目录、附加文件不过滤）
            if (
                    not is_bluray_dir
                    and not self.__is_subtitle_file(item)
                    and not self.__is_audio_file(item)
            ):
                if not self.__is_media_file(item):
                    return False
                if not self.__is_allow_filesize(item, min_filesize):
                    return False
            # 回收站及隐藏的文件不处理
            if (
                    item.path.find("/@Recycle/") != -1
                    or item.path.find("/#recycle/") != -1
                    or item.path.find("/.") != -1
                    or item.path.find("/@eaDir") != -1
            ):
                logger.debug(f"{item.path} 是回收站或隐藏的文件")
                return False
            # 整理屏蔽词不处理
            if self._is_blocked_by_exclude_words(
                    item.path, transfer_exclude_words
            ):
                return False
            return True

        try:
            # 获取经过筛选后的待整理文件项列表
            file_items = self.__get_trans_fileitems(fileitem, predicate=_filter)
        except OperationInterrupted:
            return False, f"{fileitem.name} 已取消"

        if not file_items:
            logger.warn(f"{fileitem.path} 没有找到可整理的媒体文件")
            return False, f"{fileitem.name} 没有找到可整理的媒体文件"

        logger.info(f"正在计划整理 {len(file_items)} 个文件...")

        # 整理所有文件
        transfer_tasks: List[TransferTask] = []
        try:
            for file_item, bluray_dir in file_items:
                if global_vars.is_system_stopped:
                    raise OperationInterrupted()
                if continue_callback and not continue_callback():
                    raise OperationInterrupted()
                file_path = Path(file_item.path)

                # 整理成功的不再处理
                if not force:
                    transferd = TransferHistoryOper().get_by_src(
                        file_item.path, storage=file_item.storage
                    )
                    if transferd:
                        if not transferd.status:
                            all_success = False
                        logger.info(
                            f"{file_item.path} 已整理过，如需重新处理，请删除整理记录。"
                        )
                        err_msgs.append(f"{file_item.name} 已整理过")
                        continue

                # 提前获取下载历史，以便获取自定义识别词
                downloadhis = DownloadHistoryOper()
                download_history = self._resolve_download_history(
                    downloadhis=downloadhis,
                    file_path=file_path,
                    bluray_dir=bluray_dir,
                    download_hash=download_hash,
                )

                if not meta:
                    subscribe_custom_words = None
                    if download_history and isinstance(download_history.note, dict):
                        # 使用source动态获取订阅
                        subscribe = SubscribeChain().get_subscribe_by_source(
                            download_history.note.get("source")
                        )
                        subscribe_custom_words = (
                            subscribe.custom_words.split("\n")
                            if subscribe and subscribe.custom_words
                            else None
                        )
                    # 文件元数据(优先使用订阅识别词)
                    file_meta = MetaInfoPath(
                        file_path, custom_words=subscribe_custom_words
                    )
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
                    begin_ep, end_ep, part = formaterHandler.split_episode(
                        file_name=file_path.name, file_meta=file_meta
                    )
                    if begin_ep is not None:
                        file_meta.begin_episode = begin_ep
                    if part is not None:
                        file_meta.part = part
                    if end_ep is not None:
                        file_meta.end_episode = end_ep

                # 获取下载Hash
                if download_history and (not downloader or not download_hash):
                    _downloader = download_history.downloader
                    _download_hash = download_history.download_hash
                else:
                    _downloader = downloader
                    _download_hash = download_hash

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
                    downloader=_downloader,
                    download_hash=_download_hash,
                    download_history=download_history,
                    manual=manual,
                    background=background,
                )
                if background:
                    if self.put_to_queue(task=transfer_task):
                        logger.info(f"{file_path.name} 已添加到整理队列")
                    else:
                        logger.debug(f"{file_path.name} 已在整理队列中，跳过")
                else:
                    # 加入列表
                    if self.__put_to_jobview(transfer_task):
                        transfer_tasks.append(transfer_task)
                    else:
                        logger.debug(f"{file_path.name} 已在整理列表中，跳过")
        except OperationInterrupted:
            return False, f"{fileitem.name} 已取消"
        finally:
            file_items.clear()
            del file_items

        # 实时整理
        if transfer_tasks:
            # 总数量
            total_num = len(transfer_tasks)
            # 已处理数量
            processed_num = 0
            # 失败数量
            fail_num = 0
            # 已完成文件
            finished_files = []

            # 启动进度
            progress = ProgressHelper(ProgressKey.FileTransfer)
            progress.start()
            __process_msg = f"开始整理，共 {total_num} 个文件 ..."
            logger.info(__process_msg)
            progress.update(value=0, text=__process_msg)
            try:
                for transfer_task in transfer_tasks:
                    if global_vars.is_system_stopped:
                        break
                    if continue_callback and not continue_callback():
                        break
                    # 更新进度
                    __process_msg = f"正在整理 （{processed_num + fail_num + 1}/{total_num}）{transfer_task.fileitem.name} ..."
                    logger.info(__process_msg)
                    progress.update(
                        value=(processed_num + fail_num) / total_num * 100,
                        text=__process_msg,
                        data={
                            "current": Path(transfer_task.fileitem.path).as_posix(),
                            "finished": finished_files,
                        },
                    )
                    try:
                        state, err_msg = self.__handle_transfer(
                            task=transfer_task, callback=self.__default_callback
                        )
                    except Exception as e:
                        logger.error(
                            f"{transfer_task.fileitem.name} 整理任务处理出现错误："
                            f"{e} - {traceback.format_exc()}"
                        )
                        self.__fail_transfer_task(transfer_task)
                        state, err_msg = False, str(e)
                    if not state:
                        all_success = False
                        logger.warn(f"{transfer_task.fileitem.name} {err_msg}")
                        err_msgs.append(f"{transfer_task.fileitem.name} {err_msg}")
                        fail_num += 1
                    else:
                        processed_num += 1
                    # 记录已完成
                    finished_files.append(Path(transfer_task.fileitem.path).as_posix())
            finally:
                transfer_tasks.clear()
                del transfer_tasks

            # 整理结束
            __end_msg = (
                f"整理队列处理完成，共整理 {total_num} 个文件，失败 {fail_num} 个"
            )
            logger.info(__end_msg)
            progress.update(value=100, text=__end_msg, data={})
            progress.end()

        error_msg = "、".join(err_msgs[:2]) + (
            f"，等{len(err_msgs)}个文件错误！" if len(err_msgs) > 2 else ""
        )
        return all_success, error_msg

    def remote_transfer(
            self,
            arg_str: str,
            channel: MessageChannel,
            userid: Union[str, int] = None,
            source: Optional[str] = None,
    ):
        """
        远程重新整理，参数 历史记录ID TMDBID|类型
        """

        def args_error():
            self.post_message(
                Notification(
                    channel=channel,
                    source=source,
                    title="请输入正确的命令格式：/redo [id] 或 /redo [id] [tmdbid/豆瓣id]|[类型]，"
                          "[id] 为整理记录编号",
                    userid=userid,
                )
            )

        if not arg_str:
            args_error()
            return
        arg_strs = str(arg_str).split()
        if len(arg_strs) not in (1, 2):
            args_error()
            return
        # 历史记录ID
        logid = arg_strs[0]
        if not logid.isdigit():
            args_error()
            return
        if len(arg_strs) == 1:
            state, errmsg = self.redo_transfer_history(int(logid))
            if not state:
                self.post_message(
                    Notification(
                        channel=channel,
                        title="手动整理失败",
                        source=source,
                        text=errmsg,
                        userid=userid,
                        link=settings.MP_DOMAIN("#/history"),
                    )
                )
            return
        # TMDBID/豆瓣ID
        id_strs = arg_strs[1].split("|")
        media_id = id_strs[0]
        if not logid.isdigit():
            args_error()
            return
        # 类型
        type_str = id_strs[1] if len(id_strs) > 1 else None
        if not type_str or type_str not in [MediaType.MOVIE.value, MediaType.TV.value]:
            args_error()
            return
        state, errmsg = self.__re_transfer(
            logid=int(logid), mtype=MediaType(type_str), mediaid=media_id
        )
        if not state:
            self.post_message(
                Notification(
                    channel=channel,
                    title="手动整理失败",
                    source=source,
                    text=errmsg,
                    userid=userid,
                    link=settings.MP_DOMAIN("#/history"),
                )
            )
            return

    @staticmethod
    def build_failed_transfer_buttons(
            history_id: Optional[int],
    ) -> Optional[List[List[dict]]]:
        """
        构建整理失败通知的操作按钮。
        """
        if not history_id:
            return None
        return [
            [
                {"text": "重试", "callback_data": f"transfer_retry_{history_id}"},
                {
                    "text": "智能助手接管",
                    "callback_data": f"transfer_ai_retry_{history_id}",
                },
            ]
        ]

    def redo_transfer_history(self, history_id: int) -> Tuple[bool, str]:
        """
        按历史记录直接重新整理，自动重新识别媒体信息。
        """
        return self.__re_transfer(logid=history_id)

    def __re_transfer(
            self, logid: int, mtype: MediaType = None, mediaid: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        根据历史记录，重新识别整理，只支持简单条件
        :param logid: 历史记录ID
        :param mtype: 媒体类型
        :param mediaid: TMDB ID/豆瓣ID
        """
        # 查询历史记录
        history: TransferHistory = TransferHistoryOper().get(logid)
        if not history:
            logger.error(f"整理记录不存在，ID：{logid}")
            return False, "整理记录不存在"
        # 按源目录路径重新整理
        src_path = Path(history.src)
        if not src_path.exists():
            return False, f"源目录不存在：{src_path}"
        # 查询媒体信息
        if mtype and mediaid:
            mediainfo = self.recognize_media(
                mtype=mtype,
                tmdbid=int(mediaid) if str(mediaid).isdigit() else None,
                doubanid=mediaid,
                episode_group=history.episode_group,
            )
            if mediainfo:
                # 更新媒体图片
                self.obtain_images(mediainfo=mediainfo)
        else:
            mediainfo = MediaChain().recognize_by_path(
                str(src_path), episode_group=history.episode_group
            )
        if not mediainfo:
            return False, f"未识别到媒体信息，类型：{mtype.value}，id：{mediaid}"
        # 重新执行整理
        logger.info(f"{src_path.name} 识别为：{mediainfo.title_year}")

        # 删除旧的已整理文件
        if history.dest_fileitem:
            # 解析目标文件对象
            dest_fileitem = FileItem(**history.dest_fileitem)
            StorageChain().delete_file(dest_fileitem)

        # 强制整理
        if history.src_fileitem:
            state, errmsg = self.do_transfer(
                fileitem=FileItem(**history.src_fileitem),
                mediainfo=mediainfo,
                download_hash=history.download_hash,
                force=True,
                background=False,
                manual=True,
            )
            if not state:
                return False, errmsg

        return True, ""

    def manual_transfer(
            self,
            fileitem: FileItem,
            target_storage: Optional[str] = None,
            target_path: Path = None,
            tmdbid: Optional[int] = None,
            doubanid: Optional[str] = None,
            mtype: MediaType = None,
            season: Optional[int] = None,
            episode_group: Optional[str] = None,
            transfer_type: Optional[str] = None,
            epformat: EpisodeFormat = None,
            min_filesize: Optional[int] = 0,
            scrape: Optional[bool] = None,
            library_type_folder: Optional[bool] = None,
            library_category_folder: Optional[bool] = None,
            force: Optional[bool] = False,
            background: Optional[bool] = False,
            downloader: Optional[str] = None,
            download_hash: Optional[str] = None,
    ) -> Tuple[bool, Union[str, list]]:
        """
        手动整理，支持复杂条件，带进度显示
        :param fileitem: 文件项
        :param target_storage: 目标存储
        :param target_path: 目标路径
        :param tmdbid: TMDB ID
        :param doubanid: 豆瓣ID
        :param mtype: 媒体类型
        :param season: 季度
        :param episode_group: 剧集组
        :param transfer_type: 整理类型
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param scrape: 是否刮削元数据
        :param library_type_folder: 是否按类型建立目录
        :param library_category_folder: 是否按类别建立目录
        :param force: 是否强制整理
        :param background: 是否后台运行
        :param downloader: 下载器名称
        :param download_hash: 下载任务哈希
        """
        logger.info(f"手动整理：{fileitem.path} ...")
        if tmdbid or doubanid:
            # 有输入TMDBID时单个识别
            # 识别媒体信息
            mediainfo: MediaInfo = MediaChain().recognize_media(
                tmdbid=tmdbid,
                doubanid=doubanid,
                mtype=mtype,
                episode_group=episode_group,
            )
            if not mediainfo:
                return (
                    False,
                    f"媒体信息识别失败，tmdbid：{tmdbid}，doubanid：{doubanid}，type: {mtype.value if mtype else None}",
                )
            else:
                # 更新媒体图片
                self.obtain_images(mediainfo=mediainfo)

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
                manual=True,
                downloader=downloader,
                download_hash=download_hash,
            )
            if not state:
                return False, errmsg

            logger.info(f"{fileitem.path} 整理完成")
            return True, ""
        else:
            # 没有输入TMDBID时，按文件识别
            state, errmsg = self.do_transfer(
                fileitem=fileitem,
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
                manual=True,
                downloader=downloader,
                download_hash=download_hash,
            )
            return state, errmsg

    def send_transfer_message(
            self,
            meta: MetaBase,
            mediainfo: MediaInfo,
            transferinfo: TransferInfo,
            season_episode: Optional[str] = None,
            username: Optional[str] = None,
    ):
        """
        发送入库成功的消息
        """
        self.post_message(
            Notification(
                mtype=NotificationType.Organize,
                ctype=ContentType.OrganizeSuccess,
                image=mediainfo.get_message_image(),
                username=username,
                link=settings.MP_DOMAIN("#/history"),
            ),
            meta=meta,
            mediainfo=mediainfo,
            transferinfo=transferinfo,
            season_episode=season_episode,
            username=username,
        )

    @staticmethod
    def _is_blocked_by_exclude_words(file_path: str, exclude_words: list) -> bool:
        """
        检查文件是否被整理屏蔽词阻止处理
        :param file_path: 文件路径
        :param exclude_words: 整理屏蔽词列表
        :return: 如果被屏蔽返回True，否则返回False
        """
        if not exclude_words:
            return False

        for keyword in exclude_words:
            if keyword and re.search(r"%s" % keyword, file_path, re.IGNORECASE):
                logger.warn(f"{file_path} 命中屏蔽词 {keyword}")
                return True
        return False

    def _can_delete_torrent(
            self, download_hash: str, downloader: str, transfer_exclude_words
    ) -> bool:
        """
        检查是否可以删除种子文件
        :param download_hash: 种子Hash
        :param downloader: 下载器名称
        :param transfer_exclude_words: 整理屏蔽词
        :return: 如果可以删除返回True，否则返回False
        """
        try:
            # 获取种子信息
            torrents = self.list_torrents(hashs=download_hash, downloader=downloader)
            if not torrents:
                return False

            # 未下载完成
            if torrents[0].progress < 100:
                return False

            # 获取种子文件列表
            torrent_files = self.torrent_files(download_hash, downloader)
            if not torrent_files:
                return False

            if not isinstance(torrent_files, list):
                torrent_files = torrent_files.data

            # 检查是否有媒体文件未被屏蔽且存在
            save_path = torrents[0].path.parent
            for file in torrent_files:
                file_path = save_path / file.name
                # 如果存在未被屏蔽的媒体文件，则不删除种子
                if (
                        file_path.suffix in self._allowed_exts
                        and not self._is_blocked_by_exclude_words(
                    file_path.as_posix(), transfer_exclude_words
                )
                        and file_path.exists()
                ):
                    return False

            # 所有媒体文件都被屏蔽或不存在，可以删除种子
            return True

        except Exception as e:
            logger.error(f"检查种子 {download_hash} 是否需要删除失败：{e}")
            return False
