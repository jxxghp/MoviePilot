"""
整理任务：整理链的进程内工作项。

TransferTask 此前住在 app/schemas/transfer.py，但它不是出网的 DTO——meta 装的是领域侧
的 MetaBase 子类，mediainfo 装的是领域侧的 MediaInfo / MusicInfo，都带行为而非纯数据。
放在 app.schemas 的代价是它没法命名自己真正装的类型：app.schemas 一旦 import 领域类型，
app.schemas -> app.schemas.transfer -> app.domain.* -> app.schemas.types -> app.schemas
就闭环，仓库自己的 test_migrated_modules_are_not_in_import_cycles 会红（已实测）。于是
两个字段只能标成 Optional[Any]，把「这里到底能放什么」这件事整个交给了口头约定。

搬到应用层就没有这个约束：app.application 允许依赖 app.domain 与 app.schemas，两个
字段因此能标出真实类型。它面向前端的投影仍是 app/schemas/transfer.py 里的
TransferJob / TransferJobTask，那两个用 app.schemas 的同名 DTO——一个是工作项，一个是
视图，分开表达之后两边都不必再迁就对方。
"""
import asyncio
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Callable, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict

from app.schemas.transfer import MetaInfo as _SchemaMetaInfo
from app.schemas.transfer import MusicInfo as _SchemaMusicInfo
from app.schemas.transfer import MusicMeta as _SchemaMusicMeta
from app.schemas.workflow import MediaInfo as _SchemaMediaInfo
from app.adapters.system.host import SystemUtils
from app.application.agent import get_prompt_manager, get_running_agent_manager
from app.domain.context import MediaInfo, MusicInfo
from app.domain.media import normalize_music_type
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.foundation import text as text_tools
from app.runtime.log import logger
from app.schemas.file import FileItem, FileURI
from app.schemas.history import DownloadHistory
from app.schemas.media import OptionalMediaIdentityMixin, resolve_media_identity
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo, TransferJob, TransferJobTask
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
    ReplyMode,
)



class TransferTask(OptionalMediaIdentityMixin, BaseModel):
    """
    文件整理任务。
    """

    # MetaBase 与 MediaInfo / MusicInfo 都是普通类而非 BaseModel，pydantic 需要显式放行
    model_config = ConfigDict(arbitrary_types_allowed=True)

    fileitem: FileItem
    meta: Optional[MetaBase] = None
    mediainfo: Optional[Union[MusicInfo, MediaInfo]] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    mtype: Optional[MediaType] = None
    target_directory: Optional[TransferDirectoryConf] = None
    target_storage: Optional[str] = None
    target_path: Optional[Path] = None
    transfer_type: Optional[str] = None
    scrape: Optional[bool] = False
    library_type_folder: Optional[bool] = False
    library_category_folder: Optional[bool] = False
    episodes_info: Optional[List[TmdbEpisode]] = None
    username: Optional[str] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    download_history: Optional[DownloadHistory] = None
    transfer_batch_id: Optional[str] = None
    manual: Optional[bool] = False
    background: Optional[bool] = True
    preview: Optional[bool] = False

    def to_dict(self):
        """
        返回字典。

        meta 与 mediainfo 用 to_dict() 而非 model_dump()：它们是领域对象，没有
        model_dump。此前这里写的是 model_dump()，仓内无人调用才一直没炸——字段类型
        标成 Any 时，这种错配静态检查也看不出来。
        """
        dicts = vars(self).copy()
        dicts["fileitem"] = self.fileitem.model_dump() if self.fileitem else None
        dicts["meta"] = self.meta.to_dict() if self.meta else None
        dicts["mediainfo"] = self.mediainfo.to_dict() if self.mediainfo else None
        dicts["target_directory"] = self.target_directory.model_dump() if self.target_directory else None
        return dicts


class TransferQueue(BaseModel):
    """
    异步整理队列信息。

    和 TransferTask 一起从 app/schemas 搬来：它装着一个 TransferTask 和一个回调函数，
    回调根本不可序列化，因此从来就不是 DTO，只是恰好和视图模型住在同一个文件里。
    """
    # 任务信息
    task: Optional[TransferTask] = None
    # 回调函数
    callback: Optional[Callable] = None
    # 整理结果
    result: Optional[TransferInfo] = None


class TransferQueueService:
    """协调整理任务登记、入队、移除和队列视图查询。"""

    def __init__(
            self,
            *,
            register_task: Callable[[TransferTask], bool],
            enqueue: Callable[[TransferQueue], None],
            before_enqueue: Callable[[TransferTask], None],
            after_enqueue: Callable[[TransferTask], None],
            remove_task: Callable[[FileItem], None],
            list_tasks: Callable[[], List[TransferJob]],
            expire_tasks: Callable[[], None],
    ) -> None:
        """保存队列用例依赖，避免 Application 服务绑定具体线程队列实现。"""
        self._register_task = register_task
        self._enqueue = enqueue
        self._before_enqueue = before_enqueue
        self._after_enqueue = after_enqueue
        self._remove_task = remove_task
        self._list_tasks = list_tasks
        self._expire_tasks = expire_tasks

    def put(self, task: TransferTask, callback: Callable) -> bool:
        """登记并入队一个整理任务，保持原有副作用顺序。"""
        if not task or not self._register_task(task):
            return False
        self._before_enqueue(task)
        self._enqueue(TransferQueue(task=task, callback=callback))
        self._after_enqueue(task)
        return True

    def remove(self, fileitem: FileItem) -> None:
        """从整理任务视图移除指定文件。"""
        if fileitem:
            self._remove_task(fileitem)

    def list(self) -> List[TransferJob]:
        """先处理失活任务，再返回当前整理作业视图。"""
        self._expire_tasks()
        return self._list_tasks()


@dataclass(frozen=True, slots=True)
class TransferFailureNotification:
    """整理失败聚合器保存的单条通知快照。"""

    media_title: str
    season_episode: str
    reason: str
    history_id: Optional[int]
    image: Optional[str]
    username: Optional[str]
    manual_identity: bool = False


def build_transfer_failure_group_key(task: TransferTask) -> str:
    """构造主程序和第三方整理路径可共同使用的失败通知分组键。"""
    media_source, media_id = resolve_media_identity(media=task.mediainfo)
    if not media_source or not media_id:
        media_source, media_id = resolve_media_identity(media=task)
    season = getattr(task.meta, "begin_season", None) if task.meta else None
    username = task.username or ""
    if media_source and media_id:
        return f"media:{media_source}:{media_id}:season:{season}:user:{username}"
    if task.download_hash:
        return f"download:{task.download_hash}:user:{username}"
    source_path = str(task.fileitem.path) if task.fileitem else ""
    parent_path = str(Path(source_path).parent) if source_path else ""
    return f"path:{parent_path or source_path}:user:{username}"


class TransferFailureNotificationAggregator:
    """在短暂静默窗口内按媒体合并整理失败通知。"""

    NOTIFICATION_DEBOUNCE_SECONDS = 30

    def __init__(self) -> None:
        """初始化分组缓冲和定时器。"""
        self._buffers: dict[str, list[TransferFailureNotification]] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}

    def schedule(
            self,
            *,
            group_key: str,
            notification: TransferFailureNotification,
            callback: Callable[[list[TransferFailureNotification]], None],
            loop: asyncio.AbstractEventLoop,
    ) -> None:
        """从整理线程安全地把失败快照加入事件循环中的聚合缓冲。"""
        loop.call_soon_threadsafe(
            self._schedule_on_loop,
            group_key,
            notification,
            callback,
            loop,
        )

    def _schedule_on_loop(
            self,
            group_key: str,
            notification: TransferFailureNotification,
            callback: Callable[[list[TransferFailureNotification]], None],
            loop: asyncio.AbstractEventLoop,
    ) -> None:
        """在所属事件循环中更新缓冲并重置静默窗口。"""
        self._buffers.setdefault(group_key, []).append(notification)
        timer = self._timers.pop(group_key, None)
        if timer:
            timer.cancel()
        self._timers[group_key] = loop.call_later(
            self.NOTIFICATION_DEBOUNCE_SECONDS,
            self.flush,
            group_key,
            callback,
        )

    def flush(
            self,
            group_key: str,
            callback: Callable[[list[TransferFailureNotification]], None],
    ) -> None:
        """发送一个分组内的聚合结果并释放缓冲。"""
        notifications = self._buffers.pop(group_key, [])
        self._timers.pop(group_key, None)
        if not notifications:
            return
        try:
            callback(notifications)
        except Exception as err:
            logger.error(f"发送整理失败聚合通知失败 (group={group_key}): {err}")


# 作业锁：JobManager 与 TransferChain 共享，保护整理作业视图。
job_lock = threading.Lock()

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
    # 记录任务最近一次状态心跳，供外部异步接管任务的失活检测使用
    _task_state_changed_at: Dict[Tuple[str, str], float] = {}
    # 记录仍由主程序整理线程直接执行的任务，避免把阻塞中的本地任务误判为失活
    _active_executions: set[Tuple[str, str]] = set()

    def __init__(self):
        self._job_view = {}
        self._season_episodes = {}
        self._meta_to_media_ids = {}
        self._task_state_changed_at = {}
        self._active_executions = set()

    @staticmethod
    def __get_meta_id(meta: MetaBase = None, season: Optional[int] = None) -> Tuple:
        """
        获取元数据ID
        """
        return meta.name, season

    @staticmethod
    def __get_media_id(media: Optional[Union[MediaInfo, MusicInfo]] = None,
                       season: Optional[int] = None) -> Tuple:
        """
        获取媒体ID；音乐额外区分实体类型，并为无远端ID的曲目构造稳定身份。
        """
        if not media:
            return None, season
        source, media_id = resolve_media_identity(media=media)
        if getattr(media, "type", None) == MediaType.MUSIC:
            music_type = normalize_music_type(
                getattr(media, "music_type", None),
            ) or MUSIC_ENTITY_RECORDING
            if source and media_id:
                return "music", source, media_id, music_type

            artists = tuple(
                text_tools.normalize_upper(artist)
                for artist in (getattr(media, "artists", None) or [])
                if text_tools.normalize_upper(artist)
            )
            if music_type == MUSIC_ENTITY_ALBUM:
                album_artist = text_tools.normalize_upper(
                    getattr(media, "album_artist", None)
                    or (artists[0] if artists else "")
                )
                album = text_tools.normalize_upper(
                    getattr(media, "album", None) or getattr(media, "title", None) or ""
                )
                return "music", "local", music_type, album_artist, album, getattr(media, "year", None)

            return (
                "music",
                "local",
                music_type,
                artists,
                text_tools.normalize_upper(getattr(media, "title", None) or ""),
                text_tools.normalize_upper(getattr(media, "album", None) or ""),
                getattr(media, "disc_number", None),
                getattr(media, "track_number", None),
            )
        return (source, media_id), season

    @staticmethod
    def __get_file_key(fileitem: FileItem) -> Optional[Tuple[str, str]]:
        """
        获取源文件唯一键，用于跨媒体作业识别同一个整理任务。
        """
        if not fileitem or not fileitem.path:
            return None
        normalized_path = (
            Path(str(fileitem.path).replace("\\", "/")).as_posix().rstrip("/") or "/"
        )
        return fileitem.storage or "local", normalized_path

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

    def get_job_id(self, task: TransferTask) -> Tuple:
        """返回任务当前所属的稳定作业身份，供作业级附加状态隔离使用。"""
        return self.__get_id(task)

    @staticmethod
    def __get_media(task: TransferTask) -> Union[_SchemaMediaInfo, _SchemaMusicInfo]:
        """
        获取媒体信息
        """
        if task.mediainfo:
            # 有媒体信息
            mediainfo = deepcopy(task.mediainfo)
            mediainfo.clear()
            if isinstance(mediainfo, MusicInfo):
                return _SchemaMusicInfo(**mediainfo.to_dict())
            return _SchemaMediaInfo(**mediainfo.to_dict())
        else:
            # 没有媒体信息
            meta: MetaBase = task.meta
            if isinstance(meta, MetaMusic):
                # 未识别的音乐按已解析元数据兜底展示；音乐年份为 int，
                # 不能复用 MediaInfo（year 为 str），否则触发 pydantic 校验异常
                return _SchemaMusicInfo(
                    title=meta.name,
                    artists=list(meta.artists or []),
                    artist=meta.artist,
                    album=meta.album,
                    album_artist=meta.album_artist,
                    year=meta.year,
                    title_year=f"{meta.name} ({meta.year})" if meta.year else meta.name,
                    media_source=meta.media_source,
                    media_id=meta.media_id,
                )
            return _SchemaMediaInfo(
                title=meta.name,
                year=meta.year,
                title_year=f"{meta.name} ({meta.year})",
                type=meta.type.value if meta.type else None,
            )

    @staticmethod
    def __get_meta(task: TransferTask) -> _SchemaMetaInfo:
        """
        获取元数据
        """
        if isinstance(task.meta, MetaMusic):
            return _SchemaMusicMeta(**task.meta.to_dict())
        return _SchemaMetaInfo(**task.meta.to_dict())

    def add_task(self, task: TransferTask, state: Optional[str] = "waiting") -> bool:
        """
        添加整理任务，自动分组到对应的作业中
        :return: True表示任务已添加，False表示任务无效或已存在（重复）
        """
        if not all([task, task.meta, task.fileitem]):
            return False
        file_key = self.__get_file_key(task.fileitem)
        if not file_key:
            return False
        with job_lock:
            __mediaid__ = self.__get_id(task)
            # 同一个源文件可能在识别前后落入不同作业，必须跨作业去重。
            if any(
                    self.__get_file_key(t.fileitem) == file_key
                    for job in self._job_view.values()
                    for t in job.tasks
            ):
                logger.debug(f"任务 {task.fileitem.name} 已存在，跳过重复添加")
                return False
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
                            self.__get_file_key(t.fileitem) == file_key
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
            self._task_state_changed_at[file_key] = monotonic()
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
        curr_task, source_job_id = self.__remove_task_with_job_id(
            task.fileitem, preserve_execution=True
        )
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
        job = self._job_view.pop(job_id, None)
        self._season_episodes.pop(job_id, None)
        if not job:
            return
        for task in job.tasks:
            file_key = self.__get_file_key(task.fileitem)
            if file_key:
                self._task_state_changed_at.pop(file_key, None)
                self._active_executions.discard(file_key)

    def __remove_done_job_groups(self, job_ids: set[Tuple]):
        """
        清理已进入终态的独立作业或关联作业组。
        """
        candidates = set(job_ids)
        for metaid, mediaids in list(self._meta_to_media_ids.items()):
            related_ids = {metaid, *mediaids}
            if not related_ids.intersection(candidates):
                continue
            if all(self.__is_job_done(job_id) for job_id in related_ids):
                for job_id in related_ids:
                    self.__pop_job(job_id)
                self._meta_to_media_ids.pop(metaid, None)
                candidates.difference_update(related_ids)

        referenced_ids = {
            job_id
            for metaid, mediaids in self._meta_to_media_ids.items()
            for job_id in {metaid, *mediaids}
        }
        for job_id in candidates - referenced_ids:
            if self.__is_job_done(job_id):
                self.__pop_job(job_id)

    def start_execution(self, task: TransferTask):
        """
        标记任务仍由主程序整理线程直接执行。

        :param task: 整理任务
        """
        if not task or not task.fileitem:
            return
        file_key = self.__get_file_key(task.fileitem)
        if not file_key:
            return
        with job_lock:
            self._active_executions.add(file_key)

    def finish_execution(self, task: TransferTask):
        """
        结束主程序整理线程对任务的直接执行标记。

        :param task: 整理任务
        """
        if not task or not task.fileitem:
            return
        file_key = self.__get_file_key(task.fileitem)
        if not file_key:
            return
        with job_lock:
            self._active_executions.discard(file_key)

    def expire_stale_running_tasks(
            self, timeout_seconds: int
    ) -> List[Tuple[FileItem, int]]:
        """
        将外部接管后长期无心跳的运行中任务标记失败并清理作业视图。

        主程序整理线程仍在直接执行的任务不会被清理，以免把阻塞中的真实任务
        误报为已终止。外部接管方可重复调用 ``running_task`` 刷新状态心跳。

        :param timeout_seconds: 失活超时秒数，小于等于 0 时禁用
        :return: 已失活任务及其无心跳秒数
        """
        if timeout_seconds <= 0:
            return []

        current_time = monotonic()
        expired: List[Tuple[FileItem, int]] = []
        affected_job_ids: set[Tuple] = set()
        with job_lock:
            for mediaid, job in self._job_view.items():
                for task in job.tasks:
                    file_key = self.__get_file_key(task.fileitem)
                    if (
                            not file_key
                            or task.state != "running"
                            or file_key in self._active_executions
                    ):
                        continue
                    updated_at = self._task_state_changed_at.get(file_key, current_time)
                    inactive_seconds = current_time - updated_at
                    if inactive_seconds < timeout_seconds:
                        continue
                    task.state = "failed"
                    self._task_state_changed_at[file_key] = current_time
                    episodes = getattr(task.meta, "episode_list", None) or []
                    if mediaid in self._season_episodes:
                        self._season_episodes[mediaid] = list(
                            set(self._season_episodes[mediaid]) - set(episodes)
                        )
                    expired.append((task.fileitem, int(inactive_seconds)))
                    affected_job_ids.add(mediaid)

            self.__remove_done_job_groups(affected_job_ids)
        return expired

    def running_task(self, task: TransferTask):
        """
        设置任务为运行中，并刷新外部异步任务的状态心跳。
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in self._job_view[__mediaid__].tasks:
                if t.fileitem == task.fileitem:
                    t.state = "running"
                    file_key = self.__get_file_key(t.fileitem)
                    if file_key:
                        self._task_state_changed_at[file_key] = monotonic()
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
                    file_key = self.__get_file_key(t.fileitem)
                    if file_key:
                        self._task_state_changed_at[file_key] = monotonic()
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
                    file_key = self.__get_file_key(t.fileitem)
                    if file_key:
                        self._task_state_changed_at[file_key] = monotonic()
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
        file_key = self.__get_file_key(task.fileitem)
        if not file_key:
            return
        with job_lock:
            for mediaid, job in self._job_view.items():
                for job_task in job.tasks:
                    if self.__get_file_key(job_task.fileitem) != file_key:
                        continue
                    if job_task.state not in ["completed", "failed"]:
                        job_task.state = "failed"
                        self._task_state_changed_at[file_key] = monotonic()
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
            self,
            fileitem: FileItem,
            preserve_execution: bool = False,
    ) -> Tuple[Optional[TransferJobTask], Optional[Tuple]]:
        """
        根据文件项移除任务，并返回任务所在的作业ID
        """
        file_key = self.__get_file_key(fileitem)
        if not file_key:
            return None, None
        with job_lock:
            for mediaid in list(self._job_view):
                job = self._job_view[mediaid]
                for task in job.tasks:
                    if self.__get_file_key(task.fileitem) == file_key:
                        job.tasks.remove(task)
                        self._task_state_changed_at.pop(file_key, None)
                        if not preserve_execution:
                            self._active_executions.discard(file_key)
                        # 如果没有作业了，则移除作业
                        if not job.tasks:
                            self._job_view.pop(mediaid)
                        # 移除季集信息
                        if mediaid in self._season_episodes:
                            episodes = getattr(task.meta, "episode_list", None) or []
                            self._season_episodes[mediaid] = list(
                                set(self._season_episodes[mediaid])
                                - set(episodes)
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
                job = self._job_view[__mediaid__]
                self.__pop_job(__mediaid__)
                return job
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
            self, media: Union[MediaInfo, MusicInfo], season: Optional[int] = None
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

    def count(self, media: Union[MediaInfo, MusicInfo], season: Optional[int] = None) -> int:
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

    def size(self, media: Union[MediaInfo, MusicInfo], season: Optional[int] = None) -> int:
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
                        if FileURI.is_local(task.fileitem.storage)
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

    def pending_total(self) -> int:
        """
        获取未到终态的任务总数。

        作业要等关联任务全部终态才整体移除,追更/分批场景下已完成任务会
        跨批次残留在视图中;批次统计若用全量 total() 会把历史任务计入
        「当前共 N 个文件」并压低进度百分比,因此只数未终态任务。
        """
        with job_lock:
            return sum(
                1
                for job in self._job_view.values()
                for task in job.tasks
                if task.state not in ("completed", "failed")
            )

    def list_jobs(self) -> List[TransferJob]:
        """
        获取所有作业的任务列表
        """
        with job_lock:
            return list(self._job_view.values())

    def season_episodes(
            self, media: Union[MediaInfo, MusicInfo], season: Optional[int] = None
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
        return get_prompt_manager().render_system_task_message(
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
            manager = get_running_agent_manager()
            if manager is None:
                logger.warning("智能助手服务未运行，跳过整理失败自动重试")
                return
            await manager.run_background_prompt(
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
