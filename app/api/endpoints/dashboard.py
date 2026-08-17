from pathlib import Path
from typing import Any, List, Optional, Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.schemas.dashboard import DashboardMemoryInfo as _SchemaDashboardMemoryInfo
from app.schemas.dashboard import DashboardSystemInfo as _SchemaDashboardSystemInfo
from app.schemas.dashboard import DownloaderInfo as _SchemaDownloaderInfo
from app.schemas.dashboard import ProcessInfo as _SchemaProcessInfo
from app.schemas.dashboard import ScheduleInfo as _SchemaScheduleInfo
from app.schemas.dashboard import ScheduleProgress as _SchemaScheduleProgress
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.dashboard import Storage as _SchemaStorage
from app.schemas.response import Response as _SchemaResponse
from app.api.response import ResponseAPIRouter
from app.chain.dashboard import DashboardChain
from app.chain.storage import StorageChain
from app.runtime.config import settings
from app.application.security.access import verify_apitoken
from app.db import get_db
from app.db.models.transferhistory import TransferHistory
from app.api.deps import get_current_active_superuser
from app.schemas.types import StorageAction
from app.application.directory import DirectoryHelper
from app.scheduler import Scheduler
from app.adapters.system.host import SystemUtils

router = ResponseAPIRouter()


def _build_statistic(db: Session, name: Optional[str] = None) -> _SchemaStatistic:
    """
    构建媒体数量统计信息。
    """
    media_statistics: Optional[List[_SchemaStatistic]] = (
        DashboardChain().media_statistic(name)
    )
    if media_statistics:
        # 汇总各媒体库统计信息
        ret_statistic = _SchemaStatistic()
        has_episode_count = False
        for media_statistic in media_statistics:
            ret_statistic.movie_count += media_statistic.movie_count or 0
            ret_statistic.tv_count += media_statistic.tv_count or 0
            ret_statistic.music_count += media_statistic.music_count or 0
            ret_statistic.user_count += media_statistic.user_count or 0
            if media_statistic.episode_count is not None:
                ret_statistic.episode_count += media_statistic.episode_count or 0
                has_episode_count = True
        if not has_episode_count:
            # 所有媒体服务都未提供剧集统计时，返回 None 供前端展示“未获取”。
            ret_statistic.episode_count = None
    else:
        ret_statistic = _SchemaStatistic()

    movie_count_month, tv_count_month, episode_count_month, music_count_month = (
        TransferHistory.monthly_media_statistics(db)
    )
    ret_statistic.movie_count_month = movie_count_month
    ret_statistic.tv_count_month = tv_count_month
    ret_statistic.episode_count_month = episode_count_month
    ret_statistic.music_count_month = music_count_month
    return ret_statistic


def _build_storage() -> _SchemaStorage:
    """
    构建本地存储空间信息。
    """
    total, available = 0, 0
    dirs = DirectoryHelper().get_dirs()
    if not dirs:
        return _SchemaStorage(total_storage=total, used_storage=total - available)
    # 下载目录按 storage、媒体库目录按 library_storage 汇总存储集合，
    # 用 set 去重存储名，避免同一存储被重复统计；
    # 各存储的 usage 内部已按磁盘（st_dev / Btrfs FSID）去重，相同磁盘的不同目录不会重复累加。
    storages = set(
        [d.storage for d in dirs if d.download_path and d.storage]
        + [d.library_storage for d in dirs if d.library_path and d.library_storage]
    )
    for _storage in storages:
        _result = StorageChain().manage_storage(storage=_storage, action=StorageAction.USAGE.value)
        _usage = _result.get("data") if _result.get("success") else None
        if _usage:
            total += _usage.get("total") or 0
            available += _usage.get("available") or 0
    return _SchemaStorage(total_storage=total, used_storage=total - available)


def _build_downloader(name: Optional[str] = None) -> _SchemaDownloaderInfo:
    """
    构建下载器统计信息。
    """
    # 下载目录空间
    download_dirs = DirectoryHelper().get_local_download_dirs()
    _, free_space = SystemUtils.space_usage(
        [Path(d.download_path) for d in download_dirs],
        btrfs_fsid_dedup=settings.BTRFS_FSID_DEDUP,
    )
    # 下载器信息
    downloader_info = _SchemaDownloaderInfo()
    transfer_infos = DashboardChain().downloader_info(name)
    if transfer_infos:
        for transfer_info in transfer_infos:
            downloader_info.download_speed += transfer_info.download_speed
            downloader_info.upload_speed += transfer_info.upload_speed
            downloader_info.download_size += transfer_info.download_size
            downloader_info.upload_size += transfer_info.upload_size
        downloader_info.free_space = free_space
    return downloader_info


@router.get("/statistic", summary="媒体数量统计", response_model=_SchemaStatistic)
def statistic(
    name: Optional[str] = None,
    db: Session = Depends(get_db),
    _: Any = Depends(get_current_active_superuser),
) -> Any:
    """
    查询媒体数量统计信息
    """
    return _build_statistic(db, name)


@router.get(
    "/statistic2", summary="媒体数量统计（API_TOKEN）", response_model=_SchemaStatistic
)
def statistic2(
    _: Annotated[str, Depends(verify_apitoken)],
    db: Session = Depends(get_db),
) -> Any:
    """
    查询媒体数量统计信息 API_TOKEN认证（?token=xxx）
    """
    return _build_statistic(db)


@router.get("/storage", summary="本地存储空间", response_model=_SchemaStorage)
def storage(_: Any = Depends(get_current_active_superuser)) -> Any:
    """
    查询本地存储空间信息
    """
    return _build_storage()


@router.get(
    "/storage2", summary="本地存储空间（API_TOKEN）", response_model=_SchemaStorage
)
def storage2(_: Annotated[str, Depends(verify_apitoken)]) -> Any:
    """
    查询本地存储空间信息 API_TOKEN认证（?token=xxx）
    """
    return _build_storage()


@router.get("/processes", summary="进程信息", response_model=List[_SchemaProcessInfo])
def processes(_: Any = Depends(get_current_active_superuser)) -> Any:
    """
    查询进程信息
    """
    return SystemUtils.processes()


@router.get("/system", summary="系统摘要信息", response_model=_SchemaDashboardSystemInfo)
def system_info(_: Any = Depends(get_current_active_superuser)) -> Any:
    """
    查询仪表板系统摘要信息
    """
    return SystemUtils.dashboard_system_info()


@router.get("/downloader", summary="下载器信息", response_model=_SchemaDownloaderInfo)
def downloader(
    name: Optional[str] = None, _: Any = Depends(get_current_active_superuser)
) -> Any:
    """
    查询下载器信息
    """
    return _build_downloader(name)


@router.get(
    "/downloader2",
    summary="下载器信息（API_TOKEN）",
    response_model=_SchemaDownloaderInfo,
)
def downloader2(_: Annotated[str, Depends(verify_apitoken)]) -> Any:
    """
    查询下载器信息 API_TOKEN认证（?token=xxx）
    """
    return _build_downloader()


@router.get("/schedule", summary="后台服务", response_model=List[_SchemaScheduleInfo])
async def schedule(_: Any = Depends(get_current_active_superuser)) -> Any:
    """
    查询后台服务信息
    """
    return Scheduler().list()


@router.get(
    "/schedule/{job_id}/progress",
    summary="后台服务进度",
    response_model=_SchemaResponse[_SchemaScheduleProgress],
)
async def schedule_progress(
    job_id: str, _: Any = Depends(get_current_active_superuser)
) -> Any:
    """
    查询指定后台服务的执行进度。
    """
    progress = Scheduler().get_progress(job_id)
    if not progress:
        return _SchemaResponse(success=False, message="后台服务不存在")
    return _SchemaResponse(success=True, data=progress.model_dump())


@router.get(
    "/schedule2",
    summary="后台服务（API_TOKEN）",
    response_model=List[_SchemaScheduleInfo],
)
async def schedule2(_: Annotated[str, Depends(verify_apitoken)]) -> Any:
    """
    查询下载器信息 API_TOKEN认证（?token=xxx）
    """
    return Scheduler().list()


@router.get(
    "/schedule2/{job_id}/progress",
    summary="后台服务进度（API_TOKEN）",
    response_model=_SchemaResponse[_SchemaScheduleProgress],
)
async def schedule_progress2(
    job_id: str, _: Annotated[str, Depends(verify_apitoken)]
) -> Any:
    """
    查询指定后台服务的执行进度 API_TOKEN认证（?token=xxx）
    """
    progress = Scheduler().get_progress(job_id)
    if not progress:
        return _SchemaResponse(success=False, message="后台服务不存在")
    return _SchemaResponse(success=True, data=progress.model_dump())


@router.get("/transfer", summary="文件整理统计", response_model=List[int])
async def transfer(
    days: Optional[int] = 7,
    db: Session = Depends(get_db),
    _: Any = Depends(get_current_active_superuser),
) -> Any:
    """
    查询文件整理统计信息
    """
    transfer_stat = await TransferHistory.async_statistic(db, days)
    return [stat[1] for stat in transfer_stat]


@router.get("/cpu", summary="获取当前CPU使用率", response_model=float)
def cpu(_: Any = Depends(get_current_active_superuser)) -> Any:
    """
    获取当前CPU使用率
    """
    return SystemUtils.cpu_usage()


@router.get("/cpu2", summary="获取当前CPU使用率（API_TOKEN）", response_model=float)
def cpu2(_: Annotated[str, Depends(verify_apitoken)]) -> Any:
    """
    获取当前CPU使用率 API_TOKEN认证（?token=xxx）
    """
    return SystemUtils.cpu_usage()


@router.get(
    "/memory",
    summary="获取当前应用与系统内存信息",
    response_model=_SchemaDashboardMemoryInfo,
)
def memory(_: Any = Depends(get_current_active_superuser)) -> Any:
    """
    获取当前应用与系统内存信息
    """
    return SystemUtils.memory_usage()


@router.get(
    "/memory2",
    summary="获取当前应用与系统内存信息（API_TOKEN）",
    response_model=_SchemaDashboardMemoryInfo,
)
def memory2(_: Annotated[str, Depends(verify_apitoken)]) -> Any:
    """
    获取当前应用与系统内存信息 API_TOKEN认证（?token=xxx）
    """
    return SystemUtils.memory_usage()


@router.get("/network", summary="获取当前网络流量", response_model=List[int])
def network(_: Any = Depends(get_current_active_superuser)) -> Any:
    """
    获取当前网络流量（上行和下行流量，单位：bytes/s）
    """
    return SystemUtils.network_usage()


@router.get(
    "/network2", summary="获取当前网络流量（API_TOKEN）", response_model=List[int]
)
def network2(_: Annotated[str, Depends(verify_apitoken)]) -> Any:
    """
    获取当前网络流量 API_TOKEN认证（?token=xxx）
    """
    return SystemUtils.network_usage()
