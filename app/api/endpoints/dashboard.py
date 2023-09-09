from pathlib import Path
from typing import Any, List

from fastapi import APIRouter, Depends
from requests import Session

from app import schemas
from app.chain.dashboard import DashboardChain
from app.core.config import settings
from app.core.security import verify_token
from app.db import get_db
from app.db.models.transferhistory import TransferHistory
from app.scheduler import Scheduler
from app.utils.string import StringUtils
from app.utils.system import SystemUtils
from app.utils.timer import TimerUtils

router = APIRouter()


@router.get("/statistic", summary="媒体数量统计", response_model=schemas.Statistic)
def statistic(db: Session = Depends(get_db),
              _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询媒体数量统计信息
    """
    media_statistic = DashboardChain(db).media_statistic()
    if media_statistic:
        return schemas.Statistic(
            movie_count=media_statistic.movie_count,
            tv_count=media_statistic.tv_count,
            episode_count=media_statistic.episode_count,
            user_count=media_statistic.user_count
        )
    else:
        return schemas.Statistic()


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


@router.get("/processes", summary="进程信息", response_model=List[schemas.ProcessInfo])
def processes(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询进程信息
    """
    return SystemUtils.processes()


@router.get("/downloader", summary="下载器信息", response_model=schemas.DownloaderInfo)
def downloader(db: Session = Depends(get_db),
               _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询下载器信息
    """
    transfer_info = DashboardChain(db).downloader_info()
    free_space = SystemUtils.free_space(Path(settings.DOWNLOAD_PATH))
    return schemas.DownloaderInfo(
        download_speed=transfer_info.download_speed,
        upload_speed=transfer_info.upload_speed,
        download_size=transfer_info.download_size,
        upload_size=transfer_info.upload_size,
        free_space=free_space
    )


@router.get("/schedule", summary="后台服务", response_model=List[schemas.ScheduleInfo])
def schedule(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询后台服务信息
    """
    # 返回计时任务
    schedulers = []
    # 去重
    added = []
    jobs = Scheduler().list()
    # 按照下次运行时间排序
    jobs.sort(key=lambda x: x.next_run_time)
    for job in jobs:
        if job.name not in added:
            added.append(job.name)
        else:
            continue
        if not StringUtils.is_chinese(job.name):
            continue
        if not job.next_run_time:
            status = "已停止"
            next_run = ""
        else:
            next_run = TimerUtils.time_difference(job.next_run_time)
            if not next_run:
                status = "正在运行"
            else:
                status = "阻塞" if job.pending else "等待"
        schedulers.append(schemas.ScheduleInfo(
            id=job.id,
            name=job.name,
            status=status,
            next_run=next_run
        ))

    return schedulers


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


@router.get("/memory", summary="获取当前内存使用量和使用率", response_model=List[int])
def memory(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    获取当前内存使用率
    """
    return SystemUtils.memory_usage()
