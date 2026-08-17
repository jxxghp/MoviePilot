import asyncio
import queue
import re
import threading
import traceback
import uuid
from copy import deepcopy
from pathlib import Path
from typing import List, Optional, Tuple, Union, Dict, Callable, Any

from app.chain import ChainBase
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.tmdb import TmdbChain
from app.runtime.config import settings, global_vars
from app.domain.context import MediaInfo, MusicInfo
from app.runtime.events import eventmanager
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfoPath
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.models.downloadhistory import DownloadHistory
from app.db.oper.systemconfig import SystemConfigOper
from app.db.oper.transferpending import TransferPendingOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.application.directory import DirectoryHelper
from app.application.formatting import FormatParser
from app.runtime.progress import ProgressHelper
from app.application.history import (add_transfer_fail, add_transfer_success,
                                     clear_transfer_failures, describe_history_gate,
                                     evaluate_history_gate, is_skip_action,
                                     record_transfer_failure)
from app.runtime.log import logger
from app.schemas.event import StorageOperSelectionEventData
from app.schemas.transfer import TransferInfo
from app.schemas.message import Message
from app.schemas.transfer import EpisodeFormat
from app.schemas.workflow import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import TransferJob
from app.schemas.tmdb import TmdbEpisode
from app.schemas.exception import OperationInterrupted
from app.schemas.types import (
    TorrentStatus,
    EventType,
    MediaType,
    ProgressKey,
    MessageType,
    NotificationChannel,
    SystemConfigKey,
    ChainEventType,
    ContentType,
    MediaSource,
)
from app.runtime.reload import ConfigReloadMixin
from app.application.transfer import (FailedRetryScheduler, JobManager,
                                      TransferQueueService, TransferTask, job_lock)
from app.chain._transfer import (EpisodeFormatMixin, FailedRetryMixin,
                               FileFilterMixin, FileKeyMixin,
                               HistoryMatchMixin, ManualHistoryMixin,
                               ScrapeBatchMixin)
from app.schemas.media import resolve_media_identity
from app.foundation.singleton import Singleton
from app.domain import episode as episode_rules

# 下载器锁
downloader_lock = threading.Lock()
# 任务锁
task_lock = threading.Lock()


class TransferChain(FileFilterMixin, ScrapeBatchMixin, EpisodeFormatMixin, HistoryMatchMixin, FileKeyMixin,
                    ManualHistoryMixin, FailedRetryMixin, ChainBase, ConfigReloadMixin, metaclass=Singleton):
    """
    文件整理处理链
    """

    CONFIG_WATCH = {
        "TRANSFER_THREADS",
    }

    def __init__(self):
        """初始化文件整理处理链。"""
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
        # 待整理文件落盘登记，用于进程重启后回放内存队列里未完成的任务
        self._pendingoper = TransferPendingOper()
        # 转移成功的文件清单
        self._success_target_files: Dict[Tuple, List[str]] = {}
        # 批次级刮削缓冲，避免同一批多文件入库重复触发目录刮削
        self._scrape_batches: Dict[str, Dict[str, Any]] = {}
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
        """配置变更时重启文件整理线程。"""
        self.__stop()
        self.__init()

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
            完成时发送消息、移除任务等
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
            # 发送通知，实时手动整理时不发
            if transferinfo.need_notify and (task.background or not task.manual):
                se_str = None
                if task.mediainfo.type == MediaType.TV:
                    season_episodes = self.jobview.season_episodes(
                        task.mediainfo, task.meta.begin_season
                    )
                    if season_episodes:
                        se_str = f"{task.meta.season} {episode_rules.format_ranges(season_episodes)}"
                    else:
                        se_str = f"{task.meta.season}"
                # 发送入库成功消息
                self.send_transfer_message(
                    meta=task.meta,
                    mediainfo=task.mediainfo,
                    transferinfo=transferinfo,
                    season_episode=se_str,
                    episodes_info=task.episodes_info,
                    username=task.username,
                )

        transferhis = TransferHistoryOper()
        target_dir_path = self.__get_transfer_target_dir_path(transferinfo)
        job_id = self.jobview.get_job_id(task)

        # 转移失败
        if not transferinfo.success:
            # 查重闸放行同路径新版本后由 overwrite_mode 判定不覆盖，是一次正常裁决而非故障：
            # 媒体库里原有版本仍然在位，写失败记录会用 add_force 顶掉原成功记录，此后该路径
            # 永远处于失败态，每个新事件都会重试并重推失败通知。此时保留原记录、不写历史、
            # 不发事件与通知、不触发重试，仅把任务置为未入库
            overwrite_declined = self._is_overwrite_declined(
                task, transferinfo, transferhis
            )
            history = None
            if overwrite_declined:
                logger.info(
                    f"{task.fileitem.name} 未入库并保留原整理记录：{transferinfo.message}"
                )
            else:
                logger.warn(f"{task.fileitem.name} 入库失败：{transferinfo.message}")

                # 累计失败次数，达到上限后查重闸不再自动放行重试
                record_transfer_failure(
                    task.fileitem.path if task.fileitem else None,
                    task.fileitem.storage if task.fileitem else None,
                    file_size=task.fileitem.size if task.fileitem else None,
                    file_modify_time=task.fileitem.modify_time if task.fileitem else None,
                    fileid=task.fileitem.fileid if task.fileitem else None,
                )

                # 新增转移失败历史记录
                history = add_transfer_fail(
                    fileitem=task.fileitem,
                    mode=transferinfo.transfer_type if transferinfo else "",
                    downloader=task.downloader,
                    download_hash=task.download_hash,
                    meta=task.meta,
                    mediainfo=task.mediainfo,
                    transferinfo=transferinfo,
                    transfer_history_oper=transferhis,
                )

                # 整理失败事件
                if self._is_media_file(task.fileitem):
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
                elif self._is_subtitle_file(task.fileitem):
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
                elif self._is_audio_file(task.fileitem):
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
                    Message(
                        mtype=MessageType.Manual,
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
            logger.info(f"{task.fileitem.name} 入库成功：{target_dir_path or ''}")

            # 整理成功即认为此前的连续失败已恢复，重置计数让后续故障重新获得完整重试额度
            clear_transfer_failures(
                task.fileitem.path if task.fileitem else None,
                task.fileitem.storage if task.fileitem else None,
            )

            # 新增task转移成功历史记录
            history = add_transfer_success(
                fileitem=task.fileitem,
                mode=transferinfo.transfer_type if transferinfo else "",
                downloader=task.downloader,
                download_hash=task.download_hash,
                meta=task.meta,
                mediainfo=task.mediainfo,
                transferinfo=transferinfo,
                transfer_history_oper=transferhis,
            )

            # task整理完成事件
            if self._is_primary_media_file(task.fileitem, task.mediainfo):
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
            elif self._is_subtitle_file(task.fileitem):
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
            elif self._is_audio_file(task.fileitem):
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
            target_files = transferinfo.file_list_new
            if target_files:
                with job_lock:
                    if self._success_target_files.get(job_id):
                        self._success_target_files[job_id].extend(target_files)
                    else:
                        self._success_target_files[job_id] = list(target_files)

            # 设置任务成功
            self.jobview.finish_task(task)

            # 登记批次级刮削目标
            self._record_scrape_target(task, transferinfo)

        # 全部整理完成且有成功的任务时，发送消息和事件
        if self.jobview.is_finished(task):
            # 更新文件清单
            with job_lock:
                transferinfo.file_list_new = list(dict.fromkeys(
                    self._success_target_files.pop(job_id, [])
                    or transferinfo.file_list_new
                    or []
                ))
            __notify()
            if not task.transfer_batch_id:
                self._send_metadata_scrape_event(task, transferinfo)

        # 只要该种子的所有任务都已整理完成，则设置种子状态为已整理
        self.__mark_torrent_completed_if_done(task.download_hash, task.downloader)

        # 移动模式，全部成功时删除空目录和种子文件
        if transferinfo.transfer_type in ["move"]:
            # 全部整理成功时
            if self.jobview.is_success(task):
                # 所有成功的业务
                tasks = self.jobview.success_tasks(
                    task.mediainfo, task.meta.begin_season
                )
                system_config_oper = SystemConfigOper()
                # 获取整理屏蔽词
                transfer_exclude_words = system_config_oper.get(
                    SystemConfigKey.TransferExcludeWords
                )
                # 挂载盘空目录清理默认开启
                delete_mounted_local_disk_empty_dirs = system_config_oper.get(
                    SystemConfigKey.MountedLocalDiskDeleteEmptyDirs
                ) is not False
                mounted_filesystem_cache: Dict[Path, bool] = {}
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
                    if (
                            not t.download_hash
                            and t.fileitem
                            and self._should_delete_empty_source_directories(
                        t,
                        delete_mounted_local_disk_empty_dirs,
                        mounted_filesystem_cache,
                    )
                    ):
                        # 删除剩余空目录
                        StorageChain().delete_media_file(t.fileitem, delete_self=False)

        return ret_status, ret_message

    def __get_transfer_target_dir_path(
            self, transferinfo: Optional[TransferInfo]
    ) -> Optional[str]:
        """
        获取整理目标目录路径，兼容 OpenList 等成功后目录项短时间不可见的存储。
        """
        if not transferinfo:
            return None
        if transferinfo.target_diritem and transferinfo.target_diritem.path:
            return transferinfo.target_diritem.path
        if transferinfo.target_item and transferinfo.target_item.path:
            return Path(transferinfo.target_item.path).parent.as_posix()
        if transferinfo.file_list_new:
            return Path(transferinfo.file_list_new[0]).parent.as_posix()
        return None

    def put_to_queue(self, task: TransferTask) -> bool:
        """
        添加到待整理队列
        :param task: 任务信息
        :return: True表示任务已添加到队列，False表示任务无效或已存在（重复）
        """
        return self._transfer_queue_service().put(task, self.__default_callback)

    def _transfer_queue_service(self) -> TransferQueueService:
        """构建保持旧队列对象和私有兼容接缝的应用服务。"""
        return TransferQueueService(
            register_task=self.__put_to_jobview,
            enqueue=self._queue.put,
            before_enqueue=self._register_scrape_batch_task,
            after_enqueue=self.__register_pending,
            remove_task=self.jobview.remove_task,
            list_tasks=self.jobview.list_jobs,
            expire_tasks=self.__expire_stale_transfer_tasks,
        )

    def replay_pending(self):
        """
        回放上次进程退出时仍未整理完的文件。

        在后台线程执行：回放要 stat 源文件，而启动期挂载可能尚未就绪甚至处于
        挂死状态，同步执行会把整个启动流程堵住。
        """
        threading.Thread(
            target=self.__replay_pending,
            name="MoviePilot-TransferReplay",
            daemon=True
        ).start()

    def __replay_pending(self):
        """
        把落盘登记的待整理文件重新送回整理入口。

        只回放「存储 + 源路径」这一最小事实，重新走完整的识别与整理流程，
        已经整理完成的由整理历史查重挡掉，因此不存在重复整理的问题。
        """
        try:
            pendings = self._pendingoper.list_all()
        except Exception as err:
            logger.error(f"读取待整理文件登记失败：{err}")
            return
        if not pendings:
            return
        logger.info(f"发现 {len(pendings)} 个上次未整理完的文件，正在重新送入整理链 ...")
        replayed = 0
        for storage, src_path in pendings:
            try:
                fileitem, should_discard = self.__build_replay_fileitem(storage, src_path)
                if not fileitem:
                    if should_discard:
                        # 源文件确认已消失，注销登记避免每次启动重复回放
                        self._pendingoper.discard(storage=storage, src_path=src_path)
                    continue
                self.do_transfer(fileitem=fileitem)
                replayed += 1
            except Exception as err:
                logger.error(f"回放待整理文件失败：{storage}:{src_path} - {err}")
        logger.info(f"✓ 待整理文件回放完成，{replayed} 个文件已重新送入整理链")

    @staticmethod
    def __build_replay_fileitem(storage: str, src_path: str) -> Tuple[Optional[FileItem], bool]:
        """
        为回放构造文件项。

        必须用 stat 的异常类型区分「文件真的没了」和「挂载暂时读不到」，不能用
        Path.exists()：它在任何 OSError 下都返回 False，会把挂载抖动
        （Transport endpoint is not connected）误判成文件消失，进而注销登记
        ——那等于在故障期间主动丢件，正是本表要防的事。
        :param storage: 存储
        :param src_path: 源文件路径，以 / 结尾表示蓝光原盘目录
        :return: (文件项, 是否应注销登记)。文件项为 None 表示本次不回放；
                 只有确认源文件已经消失时才注销登记
        """
        # 蓝光原盘目录在登记时保留了尾部斜杠，这里据此还原类型
        is_dir = src_path.endswith("/")
        path = Path(src_path)
        size, modify_time = None, None
        if storage == "local":
            try:
                file_stat = path.stat()
                size, modify_time = file_stat.st_size, file_stat.st_mtime
            except FileNotFoundError:
                logger.info(f"待整理文件已不存在，注销登记：{src_path}")
                return None, True
            except OSError as err:
                # 挂载未就绪或无响应属于暂时性故障，保留登记等下次启动再回放
                logger.warn(f"读取待整理文件失败，保留登记等待下次回放：{src_path} - {err}")
                return None, False
        return FileItem(
            storage=storage,
            path=src_path if is_dir else path.as_posix(),
            type="dir" if is_dir else "file",
            name=path.name,
            basename=path.stem,
            extension=path.suffix[1:] if not is_dir else None,
            size=size,
            modify_time=modify_time,
        ), False

    def __register_pending(self, task: TransferTask):
        """
        落盘登记一个待整理文件，登记失败不影响正常入队。
        :param task: 任务信息
        """
        fileitem = task.fileitem if task else None
        if not fileitem or not fileitem.path:
            return
        try:
            self._pendingoper.register(storage=fileitem.storage, src_path=fileitem.path)
        except Exception as err:
            # 登记只是重启后的补救手段，失败不能阻断正常整理
            logger.debug(f"登记待整理文件失败: {fileitem.path} - {err}")

    def __discard_pending(self, task: TransferTask):
        """
        注销一个待整理文件登记，整理到达终态（成功或失败）时调用。

        失败的文件不靠本表回放：整理历史里已有失败记录，分发器的历史门控会按
        重试预算重新送入整理链；留在本表反而会每次重启都重复回放。
        :param task: 任务信息
        """
        fileitem = task.fileitem if task else None
        if not fileitem or not fileitem.path:
            return
        try:
            self._pendingoper.discard(storage=fileitem.storage, src_path=fileitem.path)
        except Exception as err:
            logger.debug(f"注销待整理文件登记失败: {fileitem.path} - {err}")

    def __put_to_jobview(self, task: TransferTask) -> bool:
        """
        添加到作业视图
        :return: True表示任务已添加，False表示任务无效或已存在（重复）
        """
        return self.jobview.add_task(task)

    def __mark_torrent_completed_if_done(
            self,
            download_hash: Optional[str],
            downloader: Optional[str],
            history_exists: bool = True,
    ):
        """
        当同一种子的任务都已结束且种子已完成下载时，回写下载器已整理标签。
        """
        if (
                not history_exists
                or not download_hash
                or not self.jobview.is_torrent_done(download_hash)
        ):
            return
        # 作业视图只包含已登记的整理任务；多集种子部分文件先下载完成时，
        # 剩余文件尚未产生任务，此时打已整理标签会使下载器轮询永久跳过
        # 剩余文件（#6009），因此必须确认种子已整体下载完成。
        if not self.__is_torrent_download_completed(download_hash, downloader):
            logger.debug(
                f"种子 {download_hash} 尚未下载完成或状态未知，暂不设置已整理标签"
            )
            return
        if not self.jobview.is_torrent_done(download_hash):
            logger.debug(
                f"种子 {download_hash} 存在新登记的整理任务，暂不设置已整理标签"
            )
            return
        self.transfer_completed(hashs=download_hash, downloader=downloader)

    def __is_torrent_download_completed(
            self, download_hash: str, downloader: Optional[str]
    ) -> bool:
        """
        检查种子在下载器中是否已完成下载；查询不到或查询失败时视为未完成，
        留待下载器定时轮询兜底，避免误打已整理标签。
        """
        try:
            torrents = self.list_torrents(hashs=download_hash, downloader=downloader)
            if not torrents:
                return False
            return all((torrent.progress or 0) >= 100 for torrent in torrents)
        except Exception as e:
            logger.error(f"检查种子 {download_hash} 下载进度失败：{e}")
            return False

    def remove_from_queue(self, fileitem: FileItem):
        """
        从待整理队列移除
        """
        self._transfer_queue_service().remove(fileitem)

    def __start_job_execution(self, task: TransferTask):
        """在作业视图支持执行租约时标记主程序任务开始执行。"""
        marker = getattr(self.jobview, "start_execution", None)
        if marker:
            marker(task)

    def __finish_job_execution(self, task: TransferTask):
        """在作业视图支持执行租约时标记主程序任务结束执行。"""
        marker = getattr(self.jobview, "finish_execution", None)
        if marker:
            marker(task)
        # 任务已到终态，落盘登记到此作废（未登记过的实时整理路径为无害空操作）
        self.__discard_pending(task)

    def __expire_stale_transfer_tasks(self):
        """清理外部接管后失去状态心跳的运行中整理任务。"""
        timeout_minutes = max(int(settings.TRANSFER_TASK_TIMEOUT), 0)
        expire_tasks = getattr(self.jobview, "expire_stale_running_tasks", None)
        expired_tasks = (
            expire_tasks(timeout_seconds=timeout_minutes * 60)
            if expire_tasks
            else []
        )
        for fileitem, inactive_seconds in expired_tasks:
            logger.error(
                f"整理任务 {fileitem.path} 已连续 {inactive_seconds // 60} 分钟无状态心跳，"
                "已标记失败并从整理队列视图清理"
            )

    def __fail_transfer_task(self, task: TransferTask):
        """
        标记异常整理任务失败并清理作业视图
        """
        self.jobview.fail_unfinished_task(task)
        self.jobview.try_remove_job(task)
        self._finish_scrape_batch_task(task)

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
                    # 批次总数 = 本批已处理数 + 未终态数。作业视图会残留上一批
                    # 已完成的任务（作业要等关联任务全部终态才移除），用全量
                    # total() 会把历史任务计入本批（如显示 8 个实际只处理 2 个），
                    # 且进度分母虚高导致百分比走不满
                    current_total = self._processed_num + self.jobview.pending_total()
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
                    self.__start_job_execution(task)
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
                    self.__finish_job_execution(task)
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
                            # 重置计数，_total_num 一并归零，否则会作为历史最大值一直
                            # 累积，令后续批次的「当前共 N 个文件」与进度百分比失真
                            self._total_num = 0
                            self._processed_num = 0
                            self._fail_num = 0

            except queue.Empty:
                # 即使队列空了，如果还有任务在运行，也不应该结束进度
                # 这部分逻辑已经在 finally 的 active_tasks == 0 中处理了
                self.__expire_stale_transfer_tasks()
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
            # 显式标注联合：下面既会赋回音乐识别结果（MusicInfo），也会赋回影视识别
            # 结果（MediaInfo），不标注时会被推断成其中一种，另一种就成了假错误
            mediainfo: Optional[Union[MediaInfo, MusicInfo]] = task.mediainfo
            mediainfo_changed = False
            need_obtain_images = False
            if not mediainfo:
                download_history = task.download_history
                # 下载用户
                if download_history:
                    task.username = download_history.username
                    # 识别媒体信息
                    history_year_conflict = self._is_movie_year_conflict(
                        task.meta, download_history
                    )
                    if (
                            download_history.media_source
                            and download_history.media_id
                            and not history_year_conflict
                    ):
                        # 下载记录中已存在识别信息。这里不再重复标注类型：函数开头
                        # 已把 mediainfo 声明为 MediaInfo | MusicInfo | None，重复
                        # 声明会遮蔽它，把音乐识别结果判成类型错误
                        mediainfo = MediaChain().recognize_media(
                            mtype=task.mtype or MediaType(download_history.type),
                            media_source=download_history.media_source,
                            media_id=download_history.media_id,
                            music_type=self._download_history_music_type(download_history),
                            episode_group=download_history.episode_group,
                        )
                        need_obtain_images = True
                        if mediainfo:
                            # 更新自定义媒体类别
                            if download_history.media_category:
                                mediainfo.category = download_history.media_category
                    else:
                        if history_year_conflict:
                            logger.info(
                                f"{task.fileitem.name} 文件年份 {task.meta.year} 与下载记录年份 "
                                f"{download_history.year} 不一致，按文件名重新识别"
                            )
                        recognize_kwargs = {"obtain_images": True}
                        if task.media_source:
                            recognize_kwargs["media_source"] = task.media_source
                        if task.mtype:
                            recognize_kwargs["mtype"] = task.mtype
                        mediainfo = MediaChain().recognize_by_meta(
                            task.meta, **recognize_kwargs
                        )
                        if mediainfo and download_history.media_category:
                            mediainfo.category = download_history.media_category
                else:
                    # 识别媒体信息
                    recognize_kwargs = {"obtain_images": True}
                    if task.media_source:
                        recognize_kwargs["media_source"] = task.media_source
                    if task.mtype:
                        recognize_kwargs["mtype"] = task.mtype
                    mediainfo = MediaChain().recognize_by_meta(
                        task.meta, **recognize_kwargs
                    )

                # 音乐必须先经过音乐元数据模块识别；远端不可用时再保留本地标签结果，
                # 避免因离线兜底提前赋值而跳过音乐识别链。
                if not mediainfo and isinstance(task.meta, MetaMusic):
                    mediainfo = self._music_info_from_meta(task.meta)

                # 按名称识别时已在识别链路补图，这里只补齐显式ID识别的场景。
                if mediainfo and need_obtain_images:
                    self.obtain_images(mediainfo=mediainfo)

                if mediainfo and task.media_source:
                    mediainfo.scrape_source = task.media_source

                if not mediainfo:
                    if task.preview:
                        return False, "未识别到媒体信息"
                    # 未识别同样是整理失败，计入重试次数：TMDB 瞬断属于可恢复故障，
                    # 但文件名永远识别不出时不能无限重试刷通知
                    record_transfer_failure(
                        task.fileitem.path if task.fileitem else None,
                        task.fileitem.storage if task.fileitem else None,
                        file_size=task.fileitem.size if task.fileitem else None,
                        file_modify_time=task.fileitem.modify_time if task.fileitem else None,
                        fileid=task.fileitem.fileid if task.fileitem else None,
                    )
                    # 新增整理失败历史记录
                    his = add_transfer_fail(
                        fileitem=task.fileitem,
                        mode=task.transfer_type,
                        meta=task.meta,
                        downloader=task.downloader,
                        download_hash=task.download_hash,
                        transfer_history_oper=transferhis,
                    )
                    self.post_message(
                        Message(
                            mtype=MessageType.Manual,
                            title=f"{task.fileitem.name} 未识别到媒体信息，无法入库！",
                            # 历史落库失败时 his 为 None（add_transfer_fail 末尾的
                            # get_by_src 查不到即返回 None），此时 /redo 无 ID 可用，
                            # 只省去这段指引而不是让整条通知连同后续的作业清理、
                            # 种子完成标记一起崩在 NoneType 上
                            text="\n".join(
                                [
                                    "原因：未识别到媒体信息",
                                    (
                                        "如果按钮不可用，可回复：\n"
                                        f"```\n/redo {his.id}\n"
                                        f"/redo {his.id} [media_source]|[media_id]|[类型]\n```\n"
                                        "自动重试或手动识别整理。"
                                        if his
                                        else ""
                                    ),
                                ]
                            ).strip(),
                            username=task.username,
                            link=settings.MP_DOMAIN("#/history"),
                            buttons=self.build_failed_transfer_buttons(
                                his.id if his else None
                            ),
                        )
                    )
                    # 任务失败，直接移除task
                    self.jobview.remove_task(task.fileitem)
                    self.__mark_torrent_completed_if_done(
                        task.download_hash, task.downloader
                    )

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

            # TMDB 仅作为辅助信息合并，不能改变原识别源的主身份和标题。
            mediainfo = MediaChain().supplement_tmdb_info(mediainfo, task.meta)
            task.mediainfo = mediainfo

            # 只有 TMDB 主源沿用历史 TMDB 标题，避免辅助 ID 改写其它识别源标题。
            if (
                    not settings.SCRAP_FOLLOW_TMDB
                    and mediainfo.media_source == MediaSource.TMDB
            ):
                transfer_history = transferhis.get_by_media_identity(
                    media_source=mediainfo.media_source.value,
                    media_id=mediainfo.media_id,
                    mtype=mediainfo.type.value,
                )
                if transfer_history and mediainfo.title != transfer_history.title:
                    mediainfo.title = transfer_history.title
                    mediainfo_changed = True

            if mediainfo_changed:
                # 更新任务信息
                task.mediainfo = mediainfo
                # 更新队列任务
                if not self.jobview.migrate_task(task):
                    logger.info(f"{task.fileitem.name} 已存在整理任务，跳过重复处理")
                    return False, f"{task.fileitem.name} 已在整理队列中"

            # 获取集数据
            if (
                    task.mediainfo.type == MediaType.TV
                    and task.mediainfo.tmdb_id
                    and not task.episodes_info
            ):
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

            if self._requires_automatic_category(task) and not task.mediainfo.category:
                # MusicInfo 无 tmdb_id 字段，但模型 __getattr__ 已兜底返回 None
                if task.mediainfo.tmdb_id:
                    error_message = "TMDB 信息未匹配到媒体分类，无法按媒体类别整理"
                else:
                    error_message = "未识别到 TMDB 辅助信息，无法按媒体类别整理"
                logger.error(f"{task.fileitem.name} {error_message}")
                if callback:
                    return callback(
                        task,
                        TransferInfo(
                            success=False,
                            fileitem=task.fileitem,
                            transfer_type=task.transfer_type,
                            message=error_message,
                        ),
                    )
                return False, error_message

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
                preview=task.preview,
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
            self._finish_scrape_batch_task(task)

    def get_queue_tasks(self) -> List[TransferJob]:
        """
        获取整理任务列表
        """
        return self._transfer_queue_service().list()

    def process(self, progress_callback: Optional[Callable[..., None]] = None) -> bool:
        """
        获取下载器中的种子列表，并执行整理

        :param progress_callback: 定时服务进度更新回调
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
                if progress_callback:
                    progress_callback(value=100, text="未配置下载器监控目录，跳过整理")
                return True

            logger.info("开始整理下载器中已经完成下载的文件 ...")
            if progress_callback:
                progress_callback(value=0, text="正在查询已完成下载任务 ...")

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
                if progress_callback:
                    progress_callback(value=100, text="没有已完成下载但未整理的任务")
                return False

            logger.info(f"获取到 {len(torrents)} 个已完成的下载任务")
            if progress_callback:
                progress_callback(
                    value=0,
                    text=f"获取到 {len(torrents)} 个已完成下载任务",
                    data={"total": len(torrents), "finished": 0},
                )

            try:
                total_num = len(torrents)
                for index, torrent in enumerate(torrents, start=1):
                    if global_vars.is_system_stopped:
                        break
                    if progress_callback:
                        torrent_name = (
                                getattr(torrent, "title", None)
                                or getattr(torrent, "name", None)
                                or torrent.hash
                        )
                        progress_callback(
                            value=(index - 1) / total_num * 100,
                            text=f"正在整理下载任务（{index}/{total_num}）{torrent_name} ...",
                            data={
                                "total": total_num,
                                "finished": index - 1,
                                "current": torrent.hash,
                            },
                        )

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
                    # 下载记录中的媒体类型作为整理类型来源，无下载记录时留空由文件后缀兜底
                    mtype: Optional[MediaType] = None
                    if downloadhis:
                        # 类型
                        try:
                            mtype = MediaType(downloadhis.type)
                        except ValueError:
                            mtype = MediaType.TV
                        # 识别媒体信息
                        mediainfo = MediaChain().recognize_media(
                            mtype=mtype,
                            media_source=downloadhis.media_source,
                            media_id=downloadhis.media_id,
                            music_type=self._download_history_music_type(downloadhis),
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
                        mtype=mtype,
                        downloader=torrent.downloader,
                        download_hash=torrent.hash,
                    )
                    if progress_callback:
                        progress_callback(
                            value=index / total_num * 100,
                            text=f"下载任务（{index}/{total_num}）整理处理完成",
                            data={"total": total_num, "finished": index},
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
        media_categories = None

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
            media_type = dir_info.media_type

            if (
                    not dir_info.media_type
                    and dir_info.download_type_folder
                    and len(relative_parts) > part_index
                    and relative_parts[part_index] in media_type_dirs
            ):
                current_root = current_root / relative_parts[part_index]
                shared_roots.add(current_root.as_posix())
                media_type = relative_parts[part_index]
                part_index += 1

            if (
                    not dir_info.media_category
                    and dir_info.download_category_folder
                    and len(relative_parts) > part_index
            ):
                category_root = current_root / relative_parts[part_index]
                shared_roots.add(category_root.as_posix())
                if media_categories is None:
                    media_categories = MediaChain().media_category() or {}
                if media_type:
                    category_names = media_categories.get(media_type, [])
                else:
                    category_names = {
                        category
                        for categories in media_categories.values()
                        for category in categories
                    }
                category_paths = sorted(
                    (Path(category).parts for category in category_names if category),
                    key=len,
                )
                for category_parts in category_paths:
                    relative_category_parts = tuple(
                        relative_parts[part_index:part_index + len(category_parts)]
                    )
                    if relative_category_parts != category_parts:
                        continue
                    category_root = current_root
                    for category_part in category_parts:
                        category_root = category_root / category_part
                        shared_roots.add(category_root.as_posix())

        return shared_roots

    def do_transfer(
            self,
            fileitem: FileItem,
            meta: MetaBase = None,
            mediainfo: Optional[Union[MediaInfo, MusicInfo]] = None,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
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
            preview: Optional[bool] = False,
            sync_extra_files: Optional[bool] = False,
            cleanup_dest_fileitem: Optional[FileItem] = None,
            continue_callback: Callable = None,
            reorganize: Optional[bool] = False,
    ) -> Tuple[bool, Union[str, dict]]:
        """
        执行一个复杂目录的整理操作
        :param fileitem: 文件项
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param mtype: 未提供媒体信息时使用的媒体类型提示
        :param media_source: 请求级识别与刮削数据源
        :param media_id: 数据源原生 ID；显式指定身份时与 media_source 成对传入
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
        :param preview: 是否仅预览
        :param reorganize: 是否清理已有成功记录后重新整理
        :param sync_extra_files: 是否在整理主视频文件时同步整理同媒体附加文件
        :param cleanup_dest_fileitem: 确认存在待整理任务后需要清理的旧目标文件
        :param continue_callback: 继续处理回调
        返回：成功标识，错误信息
        """
        explicit_identity = media_source is not None or media_id is not None
        normalized_source, normalized_media_id = resolve_media_identity(
            media_source=media_source,
            media_id=media_id,
        )
        if explicit_identity and (
                not normalized_source or not normalized_media_id
        ):
            return False, "整理任务需要同时提供有效的 media_source 和 media_id"
        if not explicit_identity and mediainfo:
            normalized_source, normalized_media_id = resolve_media_identity(
                media=mediainfo
            )
        media_source = normalized_source
        media_id = normalized_media_id
        if explicit_identity and not mediainfo:
            mediainfo = MediaChain().recognize_media(
                mtype=mtype,
                media_source=media_source,
                media_id=media_id,
                music_type=getattr(meta, "music_type", None),
            )
            if not mediainfo:
                return False, (
                    "未识别到媒体信息，"
                    f"media_source：{media_source}，media_id：{media_id}"
                )

        # 是否全部成功
        all_success = True
        transfer_batch_id = str(uuid.uuid4())
        batch_mtype = getattr(mediainfo, "type", None)
        if batch_mtype in (None, MediaType.UNKNOWN):
            batch_mtype = mtype
        if preview:
            # 预览模式始终同步执行，避免进入异步队列
            background = False
        # 自定义格式
        has_episode_format_template = bool(epformat and epformat.format)
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
        matched_episode_format_template = False

        def _build_file_meta(
                source_path: Path,
                custom_word_list: Optional[List[str]] = None,
        ) -> Optional[MetaBase]:
            """
            构建整理任务使用的文件元数据，并应用手动季集/自定义格式覆盖。
            """
            built_meta = deepcopy(meta) if meta else _build_path_meta(
                source_path, custom_word_list=custom_word_list
            )
            if not built_meta:
                return None
            if not meta:
                # _build_path_meta 已经应用过手动季集/自定义格式覆盖；
                # 这里避免再次偏移集数，导致手动整理的集数偏移翻倍。
                return built_meta
            return _apply_meta_overrides(built_meta, source_path)

        def _has_reliable_video_source() -> bool:
            """
            是否存在可靠的影视类型来源；存在时音频按附加音轨解析，
            避免影视场景的音频文件误入音乐识别。
            """
            if batch_mtype is not None:
                return batch_mtype != MediaType.MUSIC
            # 预载媒体信息为非音乐时，整批整理视为影视上下文
            return mediainfo is not None and not isinstance(mediainfo, MusicInfo)

        def _build_path_meta(
                source_path: Path,
                custom_word_list: Optional[List[str]] = None,
                force_video: Optional[bool] = False,
        ) -> Optional[MetaBase]:
            """
            从文件路径识别媒体信息，用于判断附加文件是否属于当前主视频。
            :param force_video: 强制按视频解析，附加文件归属匹配专用，避免音乐判定干扰归属比较
            """
            # 音频后缀且无可靠影视类型来源时按音乐解析，走 MusicBrainz 识别链
            if (
                    not force_video
                    and source_path.suffix.lower() in self._audio_exts
                    and not _has_reliable_video_source()
            ):
                path_meta = MediaChain.read_path_meta(source_path)
            else:
                # 影视场景附加音轨（如评论音轨）强制按视频解析，保留季集归属
                path_meta = MetaInfoPath(
                    source_path, custom_words=custom_word_list, force_video=True
                )
            if not path_meta:
                return None
            return _apply_meta_overrides(path_meta, source_path)

        def _apply_meta_overrides(
                current_meta: MetaBase, source_path: Path
        ) -> Optional[MetaBase]:
            """
            应用手动传入的季集覆盖和自定义识别格式。
            """
            # 合并季
            if season is not None:
                current_meta.begin_season = season

            # 自定义识别
            if formaterHandler:
                # 开始集、结束集、PART
                begin_ep, end_ep, part = formaterHandler.split_episode(
                    file_name=source_path.name, file_meta=current_meta
                )
                if begin_ep is not None:
                    current_meta.begin_episode = begin_ep
                if part is not None:
                    current_meta.part = part
                if end_ep is not None:
                    current_meta.end_episode = end_ep

            return current_meta

        def _is_allowed_transfer_item(item: FileItem, is_bluray_dir: bool) -> bool:
            """
            判断候选文件项是否允许进入整理规划。

            :return: True 表示保留，False 表示排除
            """
            nonlocal matched_episode_format_template
            if continue_callback and not continue_callback():
                raise OperationInterrupted()
            # 存在集数定位模板时，模板匹配结果作为手动整理的硬过滤条件。
            if has_episode_format_template and formaterHandler:
                if not formaterHandler.match(item.name):
                    return False
                matched_episode_format_template = True
            if batch_mtype == MediaType.MUSIC:
                # 明确的音乐批次只接收音频主文件，避免混合下载目录中的视频或字幕
                # 被音乐身份和命名模板整理进音乐库。
                if not self._is_media_file(item, batch_mtype):
                    return False
                if not self._is_allow_filesize(item, min_filesize):
                    return False
            # 过滤后缀和大小（蓝光目录、附加文件不过滤）
            elif (
                    not is_bluray_dir
                    and not self._is_subtitle_file(item)
                    and not self._is_audio_file(item)
            ):
                if not self._is_media_file(item, batch_mtype):
                    return False
                if not self._is_allow_filesize(item, min_filesize):
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

        def _keep_candidate_item(item: FileItem, is_bluray_dir: bool) -> bool:
            """
            收集候选文件时仅检查中断状态，不套用整理业务过滤。
            """
            if continue_callback and not continue_callback():
                raise OperationInterrupted()
            return True

        def _collect_candidate_file_items() -> List[Tuple[FileItem, bool]]:
            """
            收集来源下的候选文件项，不在此阶段套用整理业务过滤。
            """
            return self.__get_trans_fileitems(fileitem, predicate=_keep_candidate_item)

        def _filter_allowed_file_items(
                candidates: List[Tuple[FileItem, bool]]
        ) -> List[Tuple[FileItem, bool]]:
            """
            将候选文件项筛选为本轮允许整理的文件项。
            """
            return [
                (candidate_item, candidate_bluray_dir)
                for candidate_item, candidate_bluray_dir in candidates
                if _is_allowed_transfer_item(candidate_item, candidate_bluray_dir)
            ]

        def _build_main_meta(
                main_fileitem: FileItem,
                main_bluray_dir: bool,
                download_history_oper: DownloadHistoryOper,
        ) -> Optional[MetaBase]:
            """
            构建主视频元数据。
            """
            main_path = Path(main_fileitem.path)
            main_download_history = self._resolve_download_history(
                downloadhis=download_history_oper,
                file_path=main_path,
                bluray_dir=main_bluray_dir,
                download_hash=download_hash,
            )
            return _build_file_meta(
                main_path,
                custom_word_list=self._get_subscribe_custom_words(main_download_history),
            )

        def _append_item(
                planned_items: List[Tuple[FileItem, bool]],
                seen_file_keys: set[Tuple[str, str]],
                item: FileItem,
                is_bluray_dir: bool,
        ) -> bool:
            """
            添加待整理文件项并去重。
            """
            file_key = self._get_file_key(item)
            if file_key in seen_file_keys:
                return False
            planned_items.append((item, is_bluray_dir))
            seen_file_keys.add(file_key)
            return True

        def _build_directory_index(
                items: List[Tuple[FileItem, bool]]
        ) -> Tuple[
            Dict[Tuple[str, str], List[FileItem]],
            Dict[Tuple[str, str], List[Tuple[FileItem, bool]]],
        ]:
            """
            基于已遍历结果构建同目录主视频和附加文件索引。
            """
            main_items_by_dir: Dict[Tuple[str, str], List[FileItem]] = {}
            extra_items_by_dir: Dict[Tuple[str, str], List[Tuple[FileItem, bool]]] = {}
            for item, is_bluray_dir in items:
                if not item or item.type != "file":
                    continue
                dir_key = self._get_file_parent_key(item)
                if not is_bluray_dir and self._is_media_file(item, batch_mtype):
                    main_items_by_dir.setdefault(dir_key, []).append(item)
                elif self._is_subtitle_file(item) or self._is_audio_file(item):
                    extra_items_by_dir.setdefault(dir_key, []).append((item, is_bluray_dir))
            return main_items_by_dir, extra_items_by_dir

        def _get_single_file_sibling_items(
                current_fileitem: FileItem,
        ) -> Tuple[List[FileItem], List[Tuple[FileItem, bool]]]:
            """
            单文件整理时只额外读取一次父目录，收集同目录主视频和附加文件。
            """
            storagechain = StorageChain()
            if not hasattr(storagechain, "get_parent_item") or not hasattr(
                    storagechain, "list_files"
            ):
                return [], []
            parent_item = storagechain.get_parent_item(current_fileitem)
            if not parent_item:
                return [], []
            main_fileitems: List[FileItem] = []
            extra_items: List[Tuple[FileItem, bool]] = []
            for item in storagechain.list_files(parent_item, recursion=False) or []:
                if not item or item.type != "file":
                    continue
                if self._is_media_file(item, batch_mtype):
                    main_fileitems.append(item)
                    continue
                if not (self._is_subtitle_file(item) or self._is_audio_file(item)):
                    continue
                if not _is_allowed_transfer_item(item, False):
                    continue
                extra_items.append((item, False))
            return main_fileitems, extra_items

        def _plan_file_items(
                items: List[Tuple[FileItem, bool]]
        ) -> Tuple[List[Tuple[FileItem, bool]], Dict[Tuple[str, str], MetaBase]]:
            """
            生成最终整理顺序：主视频优先，同名附加文件跟随，剩余附加文件最后处理。
            """
            if not items:
                return [], {}

            download_history_oper = DownloadHistoryOper()
            inherited_map: Dict[Tuple[str, str], MetaBase] = {}
            main_items_by_dir, extra_items_by_dir = _build_directory_index(items)
            main_items = [
                (item, is_bluray_dir)
                for item, is_bluray_dir in items
                if item
                   and (
                           is_bluray_dir
                           or (
                                   item.type == "file"
                                   and self._is_media_file(item, batch_mtype)
                           )
                   )
            ]

            single_file_mode = len(items) == 1 and fileitem.type == "file"
            if single_file_mode:
                current_item, current_bluray_dir = items[0]
                if current_item.type == "file":
                    sibling_main_items, sibling_extra_items = _get_single_file_sibling_items(
                        current_item
                    )
                    current_dir_key = self._get_file_parent_key(current_item)
                    if not current_bluray_dir and self._is_media_file(
                            current_item, batch_mtype
                    ):
                        main_items = [(current_item, current_bluray_dir)]
                        main_items_by_dir[current_dir_key] = [current_item]
                        extra_items_by_dir[current_dir_key] = sibling_extra_items
                    elif self._is_subtitle_file(current_item) or self._is_audio_file(current_item):
                        related_main_file_key = self._get_related_main_file_key(
                            extra_fileitem=current_item,
                            main_fileitems=sibling_main_items,
                        )
                        related_main_fileitem = next(
                            (
                                main_item
                                for main_item in sibling_main_items
                                if self._get_file_key(main_item) == related_main_file_key
                            ),
                            None,
                        )
                        if related_main_fileitem:
                            main_meta = _build_main_meta(
                                related_main_fileitem,
                                False,
                                download_history_oper,
                            )
                            if main_meta:
                                inherited_map[self._get_file_key(current_item)] = deepcopy(main_meta)
                        return list(items), inherited_map

            if not main_items:
                return list(items), inherited_map

            planned_items: List[Tuple[FileItem, bool]] = []
            seen_file_keys: set[Tuple[str, str]] = set()
            extra_meta_cache: Dict[Tuple[str, Tuple[str, ...]], Optional[MetaBase]] = {}

            def _get_cached_extra_meta(
                    extra_path: Path,
                    custom_word_list: Optional[List[str]],
            ) -> Optional[MetaBase]:
                """
                同一组识别词下的附加文件只解析一次。
                """
                custom_words_key = tuple(custom_word_list or [])
                cache_key = (extra_path.as_posix(), custom_words_key)
                if cache_key not in extra_meta_cache:
                    # 归属匹配专用视频解析：此处目的是判断附加文件是否跟随主视频，
                    # 若按音乐解析会导致影视目录内的音频无法与主视频比较归属
                    extra_meta_cache[cache_key] = _build_path_meta(
                        extra_path,
                        custom_word_list=list(custom_words_key) or None,
                        force_video=True,
                    )
                return extra_meta_cache[cache_key]

            for main_item, main_bluray_dir in main_items:
                _append_item(planned_items, seen_file_keys, main_item, main_bluray_dir)
                if main_bluray_dir or not self._is_media_file(
                        main_item, batch_mtype
                ):
                    continue

                main_path = Path(main_item.path)
                main_download_history = self._resolve_download_history(
                    downloadhis=download_history_oper,
                    file_path=main_path,
                    bluray_dir=main_bluray_dir,
                    download_hash=download_hash,
                )
                subscribe_custom_words = self._get_subscribe_custom_words(
                    main_download_history
                )
                main_meta = _build_file_meta(
                    main_path,
                    custom_word_list=subscribe_custom_words,
                )
                if not main_meta:
                    continue

                dir_key = self._get_file_parent_key(main_item)
                main_fileitems = main_items_by_dir.get(dir_key) or [main_item]
                main_file_key = self._get_file_key(main_item)
                for extra_item, extra_bluray_dir in extra_items_by_dir.get(dir_key, []):
                    if self._get_file_key(extra_item) in seen_file_keys:
                        continue
                    related_main_file_key = self._get_related_main_file_key(
                        extra_fileitem=extra_item,
                        main_fileitems=main_fileitems,
                    )
                    if related_main_file_key:
                        if related_main_file_key == main_file_key:
                            if _append_item(
                                    planned_items,
                                    seen_file_keys,
                                    extra_item,
                                    extra_bluray_dir,
                            ):
                                inherited_map[self._get_file_key(extra_item)] = deepcopy(main_meta)
                        continue

                    if single_file_mode or not sync_extra_files:
                        continue

                    extra_meta = _get_cached_extra_meta(
                        Path(extra_item.path),
                        subscribe_custom_words,
                    )
                    if not self._is_same_media_meta(main_meta, extra_meta):
                        continue
                    if _append_item(
                            planned_items,
                            seen_file_keys,
                            extra_item,
                            extra_bluray_dir,
                    ):
                        inherited_map[self._get_file_key(extra_item)] = deepcopy(extra_meta)

            for item, is_bluray_dir in items:
                _append_item(planned_items, seen_file_keys, item, is_bluray_dir)

            return planned_items, inherited_map

        candidate_file_items: List[Tuple[FileItem, bool]] = []
        try:
            candidate_file_items = _collect_candidate_file_items()
            file_items = _filter_allowed_file_items(candidate_file_items)
        except OperationInterrupted:
            return False, f"{fileitem.name} 已取消"
        finally:
            candidate_file_items.clear()

        if not file_items:
            if has_episode_format_template and not matched_episode_format_template:
                logger.info(f"{fileitem.path} 未匹配到集数定位模板，跳过整理")
                if preview:
                    return True, {
                        "summary": {"total": 0, "success": 0, "failed": 0},
                        "items": [],
                        "message": "",
                    }
                return True, ""
            logger.warn(f"{fileitem.path} 没有找到可整理的媒体文件")
            return False, f"{fileitem.name} 没有找到可整理的媒体文件"

        file_items, inherited_meta_map = _plan_file_items(file_items)

        planned_file_count = len(file_items)
        if cleanup_dest_fileitem and planned_file_count and not preview:
            state = StorageChain().delete_media_file(cleanup_dest_fileitem)
            if not state:
                return False, f"{cleanup_dest_fileitem.path} 删除失败"

        if preview:
            logger.info(f"正在预览 {planned_file_count} 个文件的整理路径...")
        else:
            logger.info(f"正在计划整理 {planned_file_count} 个文件...")

        # 整理所有文件
        transfer_tasks: List[TransferTask] = []
        skipped_history_count = 0
        skipped_torrents = set()
        try:
            for file_item, bluray_dir in file_items:
                if global_vars.is_system_stopped:
                    raise OperationInterrupted()
                if continue_callback and not continue_callback():
                    raise OperationInterrupted()
                file_path = Path(file_item.path)

                # 自动整理按 app/application/history.py 的统一判定去重（失败记录放行重试、
                # 成功但源文件已变化放行交 overwrite_mode 决断）；手动整理可清理失败记录，
                # 或按用户确认清理成功记录。
                if (not force or reorganize) and not preview:
                    transfer_history_oper = TransferHistoryOper()
                    transferd = self._get_manual_transfer_history(
                        fileitem=file_item,
                        transfer_history_oper=transfer_history_oper,
                        include_move_dest=manual and reorganize,
                    )
                    if transferd:
                        should_reorganize = manual and (
                                reorganize or not transferd.status
                        )
                        if should_reorganize:
                            state, message = self._delete_manual_transfer_history(
                                history=transferd,
                                transfer_history_oper=transfer_history_oper,
                            )
                            if not state:
                                all_success = False
                                logger.error(message)
                                err_msgs.append(message)
                                continue
                            logger.info(
                                f"{file_item.path} 已清理旧整理记录，继续重新整理。"
                            )
                            transferd = None

                    if transferd:
                        history_description = describe_history_gate(
                            transferd,
                            file_size=file_item.size,
                            file_modify_time=file_item.modify_time,
                            fileid=file_item.fileid,
                        )
                        if not manual:
                            # 自动路径（目录监控、下载器轮询）与监控分发共用同一套判定，
                            # 否则监控层刚放行的失败重试与升级请求会在这里被全额收回
                            gate_action = evaluate_history_gate(
                                transferd,
                                file_size=file_item.size,
                                file_modify_time=file_item.modify_time,
                                fileid=file_item.fileid,
                            )
                            if not is_skip_action(gate_action):
                                logger.info(
                                    f"{file_item.path} 命中"
                                    f"{history_description}"
                                    f"，重新送入整理"
                                )
                                transferd = None

                        if transferd:
                            skipped_history_count += 1
                            if not transferd.status:
                                all_success = False
                            # 失败记录能走到这里说明重试次数已用尽，此时同样要打已整理标签让种子
                            # 退出轮询，否则下载器每一轮都会重新扫描并在这里被拦一次，空转且刷屏
                            candidate_hash = download_hash or transferd.download_hash
                            candidate_downloader = downloader or transferd.downloader
                            if candidate_hash and candidate_downloader:
                                skipped_torrents.add(
                                    (candidate_hash, candidate_downloader)
                                )
                            logger.info(
                                f"{file_item.path} 已整理过（"
                                f"{history_description}"
                                f"），如需重新处理，请删除整理记录。"
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

                history_music_meta, history_music_info = self._restore_music_download_context(
                    download_history=download_history,
                    file_path=file_path,
                )

                if not meta:
                    # 文件元数据(优先使用订阅识别词)
                    inherited_meta = inherited_meta_map.get(
                        self._get_file_key(file_item)
                    )
                    if history_music_meta:
                        file_meta = history_music_meta
                    elif inherited_meta:
                        file_meta = deepcopy(inherited_meta)
                    else:
                        file_meta = _build_file_meta(
                            file_path,
                            custom_word_list=self._get_subscribe_custom_words(download_history),
                        )
                else:
                    file_meta = _build_file_meta(file_path)

                if not file_meta:
                    all_success = False
                    logger.error(f"{file_path.name} 无法识别有效信息")
                    err_msgs.append(f"{file_path.name} 无法识别有效信息")
                    continue

                # 获取下载Hash
                if download_history and (not downloader or not download_hash):
                    _downloader = download_history.downloader
                    _download_hash = download_history.download_hash
                else:
                    _downloader = downloader
                    _download_hash = download_hash

                # 自动整理预载的媒体信息来自整条下载历史；电影合集内文件年份冲突时逐文件识别。
                task_mediainfo = mediainfo or history_music_info
                if not task_mediainfo and isinstance(file_meta, MetaMusic):
                    # 无标签音频按目录级专辑匹配补齐曲目身份，命中结果带缓存不会逐文件重复请求
                    file_meta, task_mediainfo = self._match_music_album_context(
                        file_item, file_path, file_meta
                    )
                if (
                        not manual
                        and self._is_movie_year_conflict(file_meta, task_mediainfo)
                ):
                    task_mediainfo = None

                # 后台整理
                transfer_task = TransferTask(
                    fileitem=file_item,
                    meta=file_meta,
                    mediainfo=task_mediainfo,
                    media_source=media_source,
                    media_id=media_id,
                    mtype=batch_mtype,
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
                    transfer_batch_id=transfer_batch_id,
                    manual=manual,
                    background=background,
                    preview=preview,
                )
                if background:
                    if self.put_to_queue(task=transfer_task):
                        logger.info(f"{file_path.name} 已添加到整理队列")
                    else:
                        logger.debug(f"{file_path.name} 已在整理队列中，跳过")
                else:
                    # 加入列表
                    if self.__put_to_jobview(transfer_task):
                        self._register_scrape_batch_task(transfer_task)
                        transfer_tasks.append(transfer_task)
                    else:
                        logger.debug(f"{file_path.name} 已在整理列表中，跳过")
        except OperationInterrupted:
            return False, f"{fileitem.name} 已取消"
        finally:
            file_items.clear()
            del file_items
            self._close_scrape_batch(transfer_batch_id)

        # 实时整理
        preview_items: List[dict] = []

        def _preview_callback(task: TransferTask, transferinfo: TransferInfo) -> Tuple[bool, str]:
            item_meta = task.meta
            item_media = task.mediainfo
            preview_items.append(
                {
                    "source": task.fileitem.path,
                    "target": transferinfo.target_item.path if transferinfo.target_item else None,
                    "target_dir": transferinfo.target_diritem.path if transferinfo.target_diritem else None,
                    "success": transferinfo.success,
                    "message": transferinfo.message,
                    "type": item_media.type.value if item_media and item_media.type else None,
                    "title": item_media.title_year if item_media else None,
                    "season": item_meta.begin_season if item_meta else None,
                    "episode": item_meta.begin_episode if item_meta else None,
                    "episode_end": item_meta.end_episode if item_meta else None,
                    "part": item_meta.part if item_meta else None,
                    "org_string": item_meta.org_string if item_meta else None,
                    "apply_words": item_meta.apply_words if item_meta else [],
                    "resource_team": item_meta.resource_team if item_meta else None,
                    "customization": item_meta.customization if item_meta else None,
                }
            )
            return transferinfo.success, transferinfo.message

        if transfer_tasks:
            # 总数量
            total_num = len(transfer_tasks)
            # 已处理数量
            processed_num = 0
            # 失败数量
            fail_num = 0
            # 已完成文件
            finished_files = []

            progress = None
            if not preview:
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
                    if not preview:
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
                        self.__start_job_execution(transfer_task)
                        state, err_msg = self.__handle_transfer(
                            task=transfer_task,
                            callback=_preview_callback if preview else self.__default_callback,
                        )
                    except Exception as e:
                        logger.error(
                            f"{transfer_task.fileitem.name} 整理任务处理出现错误："
                            f"{e} - {traceback.format_exc()}"
                        )
                        if not preview:
                            self.__fail_transfer_task(transfer_task)
                        state, err_msg = False, str(e)
                    finally:
                        self.__finish_job_execution(transfer_task)
                    if not state:
                        all_success = False
                        logger.warn(f"{transfer_task.fileitem.name} {err_msg}")
                        err_msgs.append(f"{transfer_task.fileitem.name} {err_msg}")
                        if preview:
                            # 预览模式不走默认回调，这里需要手动收敛任务状态，避免残留 running
                            self.jobview.fail_task(transfer_task)
                            self.jobview.try_remove_job(transfer_task)
                        if preview and (
                                not preview_items or preview_items[-1].get("source") != transfer_task.fileitem.path):
                            preview_items.append(
                                {
                                    "source": transfer_task.fileitem.path,
                                    "target": None,
                                    "target_dir": None,
                                    "success": False,
                                    "message": err_msg,
                                    "type": None,
                                    "title": None,
                                    "season": transfer_task.meta.begin_season if transfer_task.meta else None,
                                    "episode": transfer_task.meta.begin_episode if transfer_task.meta else None,
                                    "episode_end": transfer_task.meta.end_episode if transfer_task.meta else None,
                                    "part": transfer_task.meta.part if transfer_task.meta else None,
                                    "org_string": transfer_task.meta.org_string if transfer_task.meta else None,
                                    "apply_words": transfer_task.meta.apply_words if transfer_task.meta else [],
                                    "resource_team": transfer_task.meta.resource_team if transfer_task.meta else None,
                                    "customization": transfer_task.meta.customization if transfer_task.meta else None,
                                }
                            )
                        fail_num += 1
                    else:
                        if preview:
                            # 预览模式手动标记完成，确保可重复预览
                            self.jobview.finish_task(transfer_task)
                            self.jobview.try_remove_job(transfer_task)
                        processed_num += 1
                    # 记录已完成
                    finished_files.append(Path(transfer_task.fileitem.path).as_posix())
            finally:
                transfer_tasks.clear()
                del transfer_tasks

            # 整理结束
            if not preview:
                __end_msg = (
                    f"整理队列处理完成，共整理 {total_num} 个文件，失败 {fail_num} 个"
                )
                logger.info(__end_msg)
                progress.update(value=100, text=__end_msg, data={})
                progress.end()

        # 下载器任务在这一轮可能因为历史记录全部命中而没有进入整理队列，
        # 这里补打一遍已整理标签，避免同一种子被重复扫描。
        if (
                skipped_history_count == planned_file_count
                and skipped_torrents
        ):
            for skipped_hash, skipped_downloader in skipped_torrents:
                logger.info(f"补充设置下载任务已整理标签：{skipped_hash}")
                self.__mark_torrent_completed_if_done(
                    skipped_hash, skipped_downloader
                )

        error_msg = "、".join(err_msgs[:2]) + (
            f"，等{len(err_msgs)}个文件错误！" if len(err_msgs) > 2 else ""
        )
        if preview:
            return all_success, {
                "summary": {
                    "total": len(preview_items),
                    "success": len([item for item in preview_items if item.get("success")]),
                    "failed": len([item for item in preview_items if not item.get("success")]),
                },
                "items": preview_items,
                "message": error_msg,
            }
        return all_success, error_msg

    def remote_transfer(
            self,
            arg_str: str,
            channel: NotificationChannel,
            userid: Union[str, int] = None,
            source: Optional[str] = None,
    ):
        """
        远程重新整理，参数为历史记录 ID，或媒体来源、原生 ID 与类型。
        """

        def args_error():
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    title="请输入正确的命令格式：/redo [id] 或 "
                          "/redo [id] [media_source]|[media_id]|[类型]，"
                          "[id] 为整理记录编号",
                    userid=userid,
                    save_history=False,
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
                    Message(
                        channel=channel,
                        title="手动整理失败",
                        source=source,
                        text=errmsg,
                        userid=userid,
                        link=settings.MP_DOMAIN("#/history"),
                        save_history=False,
                    )
                )
            return
        # 显式媒体身份固定为来源、原生 ID 和媒体类型三个字段。
        id_strs = arg_strs[1].split("|")
        if len(id_strs) != 3:
            args_error()
            return
        media_source, media_id, type_str = id_strs
        try:
            normalized_source = MediaSource(media_source)
        except ValueError:
            args_error()
            return
        if not type_str or type_str not in [
            MediaType.MOVIE.value,
            MediaType.TV.value,
            MediaType.MUSIC.value,
        ]:
            args_error()
            return
        state, errmsg = self._re_transfer(
            logid=int(logid),
            mtype=MediaType(type_str),
            media_source=normalized_source,
            media_id=media_id,
        )
        if not state:
            self.post_message(
                Message(
                    channel=channel,
                    title="手动整理失败",
                    source=source,
                    text=errmsg,
                    userid=userid,
                    link=settings.MP_DOMAIN("#/history"),
                    save_history=False,
                )
            )
            return

    def manual_transfer(
            self,
            fileitem: FileItem,
            target_storage: Optional[str] = None,
            target_path: Path = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
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
            preview: Optional[bool] = False,
            sync_extra_files: Optional[bool] = True,
            cleanup_dest_fileitem: Optional[FileItem] = None,
            reorganize: Optional[bool] = False,
            music_type: Optional[str] = None,
    ) -> Tuple[bool, Union[str, dict]]:
        """
        手动整理，支持复杂条件，带进度显示
        :param fileitem: 文件项
        :param target_storage: 目标存储
        :param target_path: 目标路径
        :param media_source: 媒体数据源
        :param media_id: 数据源原生 ID，必须与 media_source 成对提供
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
        :param preview: 是否仅预览
        :param reorganize: 是否清理已有成功记录后重新整理
        :param sync_extra_files: 是否同步整理同媒体附加文件
        :param cleanup_dest_fileitem: 确认存在待整理任务后需要清理的旧目标文件
        :param music_type: 音乐实体类型；为保持位置参数兼容，必须追加在签名末尾
        """
        logger.info(f"手动整理：{fileitem.path} ...")
        explicit_identity = media_source is not None or media_id is not None
        if explicit_identity and (not media_source or not media_id):
            return False, "手动整理需要同时提供 media_source 和 media_id"
        if media_source and media_id:
            # 有输入媒体ID时预先识别，音乐与影视统一走 recognize_media 按类型分发
            mediainfo = MediaChain().recognize_media(
                media_source=media_source,
                media_id=media_id,
                music_type=music_type,
                mtype=mtype,
                episode_group=episode_group,
            )
            if not mediainfo:
                return (
                    False,
                    f"媒体信息识别失败，media_source：{media_source}，media_id：{media_id}，"
                    f"type: {mtype.value if mtype else None}",
                )
            if media_source and not isinstance(mediainfo, MusicInfo):
                mediainfo.scrape_source = media_source
            if not isinstance(mediainfo, MusicInfo):
                self.obtain_images(mediainfo=mediainfo)

            # 开始整理
            state, errmsg = self.do_transfer(
                fileitem=fileitem,
                target_storage=target_storage,
                target_path=target_path,
                mediainfo=mediainfo,
                mtype=mtype,
                media_source=media_source,
                media_id=media_id,
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
                preview=preview,
                reorganize=reorganize,
                sync_extra_files=sync_extra_files,
                cleanup_dest_fileitem=cleanup_dest_fileitem,
            )
            if not state:
                return False, errmsg

            logger.info(f"{fileitem.path} 整理完成")
            return True, errmsg if preview else ""
        else:
            # 没有输入媒体ID时，按文件识别
            state, errmsg = self.do_transfer(
                fileitem=fileitem,
                target_storage=target_storage,
                target_path=target_path,
                media_source=media_source,
                mtype=mtype,
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
                preview=preview,
                reorganize=reorganize,
                sync_extra_files=sync_extra_files,
                cleanup_dest_fileitem=cleanup_dest_fileitem,
            )
            return state, errmsg

    def send_transfer_message(
            self,
            meta: MetaBase,
            mediainfo: Union[MediaInfo, MusicInfo],
            transferinfo: TransferInfo,
            season_episode: Optional[str] = None,
            episodes_info: Optional[List[TmdbEpisode]] = None,
            username: Optional[str] = None,
    ):
        """
        发送入库成功的消息
        :param meta: 文件元数据
        :param mediainfo: 识别的媒体信息
        :param transferinfo: 文件整理信息
        :param season_episode: 已入库季集文本
        :param episodes_info: 当前季的全部集信息
        :param username: 用户名
        """
        self.post_message(
            Message(
                mtype=MessageType.Organize,
                ctype=ContentType.OrganizeSuccess,
                image=mediainfo.get_message_image(),
                username=username,
                link=settings.MP_DOMAIN("#/history"),
            ),
            meta=meta,
            mediainfo=mediainfo,
            transferinfo=transferinfo,
            season_episode=season_episode,
            episodes_info=episodes_info,
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
