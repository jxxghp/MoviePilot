from typing import Any, List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.dashboard import DashboardChain
from app.core.config import settings
from app.core.security import verify_token, verify_uri_token
from app.db import get_db
from app.db.models.transferhistory import TransferHistory
from app.scheduler import Scheduler
from app.utils.system import SystemUtils

router = APIRouter()


@router.get("/statistic", summary="媒体数量统计", response_model=schemas.Statistic)
def statistic(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询媒体数量统计信息
    """
    media_statistics: Optional[List[schemas.Statistic]] = DashboardChain().media_statistic()
    if media_statistics:
        # 汇总各媒体库统计信息
        ret_statistic = schemas.Statistic()
        for media_statistic in media_statistics:
            ret_statistic.movie_count += media_statistic.movie_count
            ret_statistic.tv_count += media_statistic.tv_count
            ret_statistic.episode_count += media_statistic.episode_count
            ret_statistic.user_count += media_statistic.user_count
        return ret_statistic
    else:
        return schemas.Statistic()


@router.get("/statistic2", summary="媒体数量统计（API_TOKEN）", response_model=schemas.Statistic)
def statistic2(_: str = Depends(verify_uri_token)) -> Any:
    """
    查询媒体数量统计信息 API_TOKEN认证（?token=xxx）
    """
    return statistic()


@router.get("/storage", summary="存储空间", response_model=schemas.Storage)
def storage(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询存储空间信息
    """
    total_storage, free_storage = SystemUtils.space_usage(settings.LIBRARY_PATHS)
    return schemas.Storage(
        total_storage=total_storage,
        used_storage=total_storage - free_storage
    )


@router.get("/storage2", summary="存储空间（API_TOKEN）", response_model=schemas.Storage)
def storage2(_: str = Depends(verify_uri_token)) -> Any:
    """
    查询存储空间信息 API_TOKEN认证（?token=xxx）
    """
    return storage()


@router.get("/processes", summary="进程信息", response_model=List[schemas.ProcessInfo])
def processes(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询进程信息
    """
    return SystemUtils.processes()


@router.get("/downloader", summary="下载器信息", response_model=schemas.DownloaderInfo)
def downloader(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询下载器信息
    """
    transfer_info = DashboardChain().downloader_info()
    free_space = SystemUtils.free_space(settings.SAVE_PATH)
    if transfer_info:
        return schemas.DownloaderInfo(
            download_speed=transfer_info.download_speed,
            upload_speed=transfer_info.upload_speed,
            download_size=transfer_info.download_size,
            upload_size=transfer_info.upload_size,
            free_space=free_space
        )
    else:
        return schemas.DownloaderInfo()


@router.get("/downloader2", summary="下载器信息（API_TOKEN）", response_model=schemas.DownloaderInfo)
def downloader2(_: str = Depends(verify_uri_token)) -> Any:
    """
    查询下载器信息 API_TOKEN认证（?token=xxx）
    """
    return downloader()


@router.get("/schedule", summary="后台服务", response_model=List[schemas.ScheduleInfo])
def schedule(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询后台服务信息
    """
    return Scheduler().list()


@router.get("/schedule2", summary="后台服务（API_TOKEN）", response_model=List[schemas.ScheduleInfo])
def schedule2(_: str = Depends(verify_uri_token)) -> Any:
    """
    查询下载器信息 API_TOKEN认证（?token=xxx）
    """
    return schedule()


@router.get("/transfer", summary="文件整理统计", response_model=List[int])
def transfer(days: int = 7, db: Session = Depends(get_db),
             _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询文件整理统计信息
    """
    transfer_stat = TransferHistory.statistic(db, days)
    return [stat[1] for stat in transfer_stat]


@router.get("/cpu", summary="获取当前CPU使用率", response_model=int)
def cpu(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    获取当前CPU使用率
    """
    return SystemUtils.cpu_usage()


@router.get("/cpu2", summary="获取当前CPU使用率（API_TOKEN）", response_model=int)
def cpu2(_: str = Depends(verify_uri_token)) -> Any:
    """
    获取当前CPU使用率 API_TOKEN认证（?token=xxx）
    """
    return cpu()


@router.get("/memory", summary="获取当前内存使用量和使用率", response_model=List[int])
def memory(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    获取当前内存使用率
    """
    return SystemUtils.memory_usage()


@router.get("/memory2", summary="获取当前内存使用量和使用率（API_TOKEN）", response_model=List[int])
def memory2(_: str = Depends(verify_uri_token)) -> Any:
    """
    获取当前内存使用率 API_TOKEN认证（?token=xxx）
    """
    return memory()
