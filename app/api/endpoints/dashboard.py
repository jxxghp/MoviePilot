from pathlib import Path
from typing import Any, List

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.dashboard import DashboardChain
from app.core.config import settings
from app.core.security import verify_token
from app.utils.system import SystemUtils

router = APIRouter()


@router.get("/statistic", summary="媒体数量统计", response_model=schemas.Statistic)
def statistic(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询媒体数量统计信息
    """
    media_statistic = DashboardChain().media_statistic()
    return schemas.Statistic(
        movie_count=media_statistic.movie_count,
        tv_count=media_statistic.tv_count,
        episode_count=media_statistic.episode_count,
        user_count=media_statistic.user_count
    )


@router.get("/storage", summary="存储空间", response_model=schemas.Storage)
def storage(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询存储空间信息
    """
    total_storage, free_storage = SystemUtils.space_usage(Path(settings.LIBRARY_PATH))
    return schemas.Storage(
        total_storage=total_storage,
        used_storage=total_storage - free_storage
    )


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
    free_space = SystemUtils.free_space(Path(settings.DOWNLOAD_PATH))
    return schemas.DownloaderInfo(
        download_speed=transfer_info.download_speed,
        upload_speed=transfer_info.upload_speed,
        download_size=transfer_info.download_size,
        upload_size=transfer_info.upload_size,
        free_space=free_space
    )
