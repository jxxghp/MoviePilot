"""进程内整理作业视图、状态心跳与作业级查询。"""

from __future__ import annotations

import threading
from copy import deepcopy
from pathlib import Path
from time import monotonic as _system_monotonic
from typing import TYPE_CHECKING, Dict, List, Optional, Protocol, Tuple, Union, cast

from app.application.transfer.projection import (
    domain_to_dict as _domain_to_dict,
)
from app.application.transfer.projection import (
    transfer_task_meta as _transfer_task_meta,
)
from app.domain.context import MediaInfo, MusicInfo
from app.domain.media import normalize_music_type
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.foundation import text as text_tools
from app.runtime.log import logger
from app.schemas.context import MediaInfo as _SchemaMediaInfo
from app.schemas.context import MetaInfo as _SchemaMetaInfo
from app.schemas.file import FileItem
from app.schemas.media import resolve_media_identity
from app.schemas.music import MusicInfo as _SchemaMusicInfo
from app.schemas.music import MusicMeta as _SchemaMusicMeta
from app.schemas.transfer import TransferJob, TransferJobTask
from app.schemas.types import MUSIC_ENTITY_ALBUM, MUSIC_ENTITY_RECORDING, MediaType

if TYPE_CHECKING:
    from app.application.transfer.models import TransferTask


def monotonic() -> float:
    """读取整理作业失活检测使用的单调时钟。"""
    return _system_monotonic()

JobId = tuple[object, ...]
FileKey = tuple[str, str]


class DirectorySize(Protocol):
    """描述本地文件或目录大小读取能力。"""

    def get_directory_size(self, path: Path) -> int:
        """返回真实本地路径占用的字节数。"""
        ...


_directory_size: Optional[DirectorySize] = None


def configure_directory_size(reader: Optional[DirectorySize]) -> None:
    """由启动组合根注入或清除本地目录大小适配器。"""
    global _directory_size
    _directory_size = reader


def _job_tasks(job: TransferJob) -> list[TransferJobTask]:
    """声明进程内作业始终使用已初始化的任务列表。"""
    return cast(list[TransferJobTask], job.tasks)


def _job_task_fileitem(task: TransferJobTask) -> FileItem:
    """声明进程内作业任务始终绑定源文件。"""
    return cast(FileItem, task.fileitem)


def _job_task_size(task: TransferJobTask) -> int:
    """按既有本地目录回退规则返回已完成任务的文件大小。"""
    fileitem = _job_task_fileitem(task)
    if fileitem.size is not None:
        return fileitem.size
    if fileitem.storage == "local":
        if _directory_size is None:
            raise RuntimeError("本地目录大小能力尚未由启动组合根配置")
        return _directory_size.get_directory_size(Path(cast(str, fileitem.path)))
    return 0


job_lock = threading.Lock()


class JobManager:
    """
    作业管理器
    task任务负责一个文件的整理，job作业负责一个媒体的整理
    """

    # 整理中的作业
    _job_view: Dict[JobId, TransferJob] = {}
    # 汇总季集清单
    _season_episodes: Dict[JobId, List[int]] = {}
    # 记录从 meta 作业迁移到 media 作业的关系，用于清理提前失败后残留的 media 作业
    _meta_to_media_ids: Dict[JobId, set[JobId]] = {}
    # 记录任务最近一次状态心跳，供外部异步接管任务的失活检测使用
    _task_state_changed_at: Dict[FileKey, float] = {}
    # 记录仍由主程序整理线程直接执行的任务，避免把阻塞中的本地任务误判为失活
    _active_executions: set[FileKey] = set()

    def __init__(self) -> None:
        """初始化当前进程内的整理作业状态。"""
        self._job_view = {}
        self._season_episodes = {}
        self._meta_to_media_ids = {}
        self._task_state_changed_at = {}
        self._active_executions = set()

    @staticmethod
    def __get_meta_id(
            meta: Optional[MetaBase] = None,
            season: Optional[int] = None,
    ) -> JobId:
        """
        获取元数据ID
        """
        return cast(MetaBase, meta).name, season

    @staticmethod
    def __get_media_id(media: Optional[Union[MediaInfo, MusicInfo]] = None,
                       season: Optional[int] = None) -> JobId:
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

    def __get_id(self, task: TransferTask) -> JobId:
        """
        获取作业ID
        """
        resolved_task = task
        meta = _transfer_task_meta(resolved_task)
        if resolved_task.mediainfo:
            return self.__get_media_id(
                media=resolved_task.mediainfo, season=meta.begin_season
            )
        return self.__get_meta_id(meta=meta, season=meta.begin_season)

    def get_job_id(self, task: TransferTask) -> JobId:
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
                return _SchemaMusicInfo(**_domain_to_dict(mediainfo))
            return _SchemaMediaInfo(**_domain_to_dict(mediainfo))
        else:
            # 没有媒体信息
            meta = _transfer_task_meta(task)
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
        return _SchemaMetaInfo(**_domain_to_dict(_transfer_task_meta(task)))

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
                    self.__get_file_key(_job_task_fileitem(t)) == file_key
                    for job in self._job_view.values()
                    for t in _job_tasks(job)
            ):
                logger.debug(f"任务 {task.fileitem.name} 已存在，跳过重复添加")
                return False
            if __mediaid__ not in self._job_view:
                self._job_view[__mediaid__] = TransferJob(
                    media=self.__get_media(task),
                    season=_transfer_task_meta(task).begin_season,
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
                # 同锁内的跨作业检查已覆盖当前作业，直接追加通过去重的任务。
                _job_tasks(self._job_view[__mediaid__]).append(
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
                self._season_episodes[__mediaid__].extend(
                    _transfer_task_meta(task).episode_list
                )
                self._season_episodes[__mediaid__] = list(
                    set(self._season_episodes[__mediaid__])
                )
            else:
                self._season_episodes[__mediaid__] = _transfer_task_meta(task).episode_list
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
            meta = _transfer_task_meta(task)
            metaid = self.__get_meta_id(meta=meta, season=meta.begin_season)
            mediaid = self.__get_id(task)
            if source_job_id == metaid and mediaid != metaid:
                with job_lock:
                    self._meta_to_media_ids.setdefault(metaid, set()).add(mediaid)
        return True

    def __is_job_done(self, job_id: JobId) -> bool:
        """
        检查指定作业是否已完成
        """
        if job_id not in self._job_view:
            return True
        return all(
            task.state in ["completed", "failed"]
            for task in _job_tasks(self._job_view[job_id])
        )

    def __pop_job(self, job_id: JobId) -> None:
        """
        移除指定作业和对应季集缓存
        """
        job = self._job_view.pop(job_id, None)
        self._season_episodes.pop(job_id, None)
        if not job:
            return
        for task in _job_tasks(job):
            file_key = self.__get_file_key(_job_task_fileitem(task))
            if file_key:
                self._task_state_changed_at.pop(file_key, None)
                self._active_executions.discard(file_key)

    def __remove_done_job_groups(self, job_ids: set[JobId]) -> None:
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

    def start_execution(self, task: TransferTask) -> None:
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

    def finish_execution(self, task: TransferTask) -> None:
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
    ) -> List[tuple[FileItem, int]]:
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
        expired: List[tuple[FileItem, int]] = []
        affected_job_ids: set[JobId] = set()
        with job_lock:
            for mediaid, job in self._job_view.items():
                for task in _job_tasks(job):
                    fileitem = _job_task_fileitem(task)
                    file_key = self.__get_file_key(fileitem)
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
                    expired.append((fileitem, int(inactive_seconds)))
                    affected_job_ids.add(mediaid)

            self.__remove_done_job_groups(affected_job_ids)
        return expired

    def running_task(self, task: TransferTask) -> None:
        """
        设置任务为运行中，并刷新外部异步任务的状态心跳。
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in _job_tasks(self._job_view[__mediaid__]):
                if t.fileitem == task.fileitem:
                    t.state = "running"
                    file_key = self.__get_file_key(_job_task_fileitem(t))
                    if file_key:
                        self._task_state_changed_at[file_key] = monotonic()
                    break

    def finish_task(self, task: TransferTask) -> None:
        """
        设置任务为完成/成功
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in _job_tasks(self._job_view[__mediaid__]):
                if t.fileitem == task.fileitem:
                    t.state = "completed"
                    file_key = self.__get_file_key(_job_task_fileitem(t))
                    if file_key:
                        self._task_state_changed_at[file_key] = monotonic()
                    break

    def fail_task(self, task: TransferTask) -> None:
        """
        设置任务为失败
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in _job_tasks(self._job_view[__mediaid__]):
                if t.fileitem == task.fileitem:
                    t.state = "failed"
                    file_key = self.__get_file_key(_job_task_fileitem(t))
                    if file_key:
                        self._task_state_changed_at[file_key] = monotonic()
                    break
            # 移除剧集信息
            if __mediaid__ in self._season_episodes:
                self._season_episodes[__mediaid__] = list(
                    set(self._season_episodes[__mediaid__])
                    - set(_transfer_task_meta(task).episode_list)
                )

    def fail_unfinished_task(self, task: TransferTask) -> None:
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
                for job_task in _job_tasks(job):
                    if self.__get_file_key(_job_task_fileitem(job_task)) != file_key:
                        continue
                    if job_task.state not in ["completed", "failed"]:
                        job_task.state = "failed"
                        self._task_state_changed_at[file_key] = monotonic()
                        if mediaid in self._season_episodes:
                            self._season_episodes[mediaid] = list(
                                set(self._season_episodes[mediaid])
                                - set(_transfer_task_meta(task).episode_list)
                            )
                    return

    def remove_task(
            self, fileitem: FileItem, *, finished_only: bool = False,
    ) -> Optional[TransferJobTask]:
        """
        按源文件移除任务；手动重做只清除已退出执行的终态视图。
        """
        task, _ = self.__remove_task_with_job_id(fileitem, finished_only=finished_only)
        return task

    def __remove_task_with_job_id(
            self,
            fileitem: FileItem,
            preserve_execution: bool = False,
            finished_only: bool = False,
    ) -> tuple[Optional[TransferJobTask], Optional[JobId]]:
        """
        根据文件项移除任务，并返回任务所在的作业ID
        """
        file_key = self.__get_file_key(fileitem)
        if not file_key:
            return None, None
        with job_lock:
            for mediaid in list(self._job_view):
                job = self._job_view[mediaid]
                for task in _job_tasks(job):
                    if self.__get_file_key(_job_task_fileitem(task)) == file_key:
                        if finished_only and (
                                task.state not in {"completed", "failed"}
                                or file_key in self._active_executions
                        ):
                            return None, None
                        _job_tasks(job).remove(task)
                        self._task_state_changed_at.pop(file_key, None)
                        if not preserve_execution:
                            self._active_executions.discard(file_key)
                        # 如果没有作业了，则移除作业
                        if not _job_tasks(job):
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

    def try_remove_job(self, task: TransferTask) -> None:
        """
        尝试移除任务对应的作业（严格检查未完成作业，线程安全）
        """
        with job_lock:
            meta = _transfer_task_meta(task)
            __metaid__ = self.__get_meta_id(meta=meta, season=meta.begin_season)
            __mediaid__ = self.__get_media_id(
                media=task.mediainfo, season=meta.begin_season
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
            meta = _transfer_task_meta(task)
            __metaid__ = self.__get_meta_id(meta=meta, season=meta.begin_season)
            __mediaid__ = self.__get_media_id(
                media=task.mediainfo, season=meta.begin_season
            )
            if __metaid__ in self._job_view:
                meta_done = all(
                    task.state in ["completed", "failed"]
                    for task in _job_tasks(self._job_view[__metaid__])
                )
            else:
                meta_done = True
            if __mediaid__ in self._job_view:
                media_done = all(
                    task.state in ["completed", "failed"]
                    for task in _job_tasks(self._job_view[__mediaid__])
                )
            else:
                media_done = True
            return meta_done and media_done

    def is_finished(self, task: TransferTask) -> bool:
        """
        检查任务对应的作业是否已完成且有成功的记录
        """
        with job_lock:
            meta = _transfer_task_meta(task)
            __metaid__ = self.__get_meta_id(meta=meta, season=meta.begin_season)
            __mediaid__ = self.__get_media_id(
                media=task.mediainfo, season=meta.begin_season
            )
            if __metaid__ in self._job_view:
                meta_finished = all(
                    task.state in ["completed", "failed"]
                    for task in _job_tasks(self._job_view[__metaid__])
                )
            else:
                meta_finished = True
            if __mediaid__ in self._job_view:
                tasks = _job_tasks(self._job_view[__mediaid__])
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
            meta = _transfer_task_meta(task)
            __metaid__ = self.__get_meta_id(meta=meta, season=meta.begin_season)
            __mediaid__ = self.__get_media_id(
                media=task.mediainfo, season=meta.begin_season
            )
            if __metaid__ in self._job_view:
                meta_success = all(
                    task.state in ["completed"]
                    for task in _job_tasks(self._job_view[__metaid__])
                )
            else:
                meta_success = True
            if __mediaid__ in self._job_view:
                media_success = all(
                    task.state in ["completed"]
                    for task in _job_tasks(self._job_view[__mediaid__])
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
                cast(str, task.download_hash)
                for job in self._job_view.values()
                for task in _job_tasks(job)
            }

    def is_torrent_done(self, download_hash: str) -> bool:
        """
        检查指定种子的所有任务是否都已完成
        """
        with job_lock:
            if any(
                    task.state not in {"completed", "failed"}
                    for job in self._job_view.values()
                    for task in _job_tasks(job)
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
                    for task in _job_tasks(job)
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
                and len(_job_tasks(self._job_view[__metaid__])) > 0
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
                for task in _job_tasks(self._job_view[__mediaid__])
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
            return _job_tasks(self._job_view[__mediaid__])

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
                    for task in _job_tasks(self._job_view[__mediaid__])
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
                    _job_task_size(task)
                    for task in _job_tasks(self._job_view[__mediaid__])
                    if task.state == "completed"
                ]
            )

    def total(self) -> int:
        """
        获取所有任务总数
        """
        with job_lock:
            return sum([len(_job_tasks(job)) for job in self._job_view.values()])

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
                for task in _job_tasks(job)
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
