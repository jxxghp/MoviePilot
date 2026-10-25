"""
汇总仪表盘所需的服务统计、系统状态与活动数据。
"""

from typing import Annotated, Any, List, Optional

from fastapi import Depends

from app.adapters.system.host import SystemUtils
from app.adapters.web.security.access import verify_apitoken
from app.api.context import get_api_runtime_config, resolve_api_runtime_config
from app.api.dependencies.auth import get_current_active_superuser
from app.api.dependencies.dashboard import sample_downloader
from app.api.dependencies.history import get_dashboard_query_service
from app.api.response import (
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
)
from app.application.configuration import ApiRuntimeConfig
from app.application.dashboard import DashboardQueryService
from app.application.scheduling import get_scheduler
from app.chain.dashboard import DashboardChain
from app.runtime.execution import run_in_threadpool
from app.schemas.dashboard import DashboardMemoryInfo as _SchemaDashboardMemoryInfo
from app.schemas.dashboard import DashboardSystemInfo as _SchemaDashboardSystemInfo
from app.schemas.dashboard import DownloaderInfo as _SchemaDownloaderInfo
from app.schemas.dashboard import ProcessInfo as _SchemaProcessInfo
from app.schemas.dashboard import ScheduleInfo as _SchemaScheduleInfo
from app.schemas.dashboard import ScheduleProgress as _SchemaScheduleProgress
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.dashboard import Storage as _SchemaStorage
from app.schemas.response import Response as _SchemaResponse

router = ResponseAPIRouter()


def _build_storage() -> _SchemaStorage:
    """
    构建本地存储空间信息。
    """
    return DashboardChain().storage()


def _build_downloader(
    name: Optional[str] = None,
    *,
    btrfs_fsid_dedup: bool = False,
) -> _SchemaDownloaderInfo:
    """
    使用共用采样口径构建下载器统计，保持普通页面的宽容来源语义。

    :param name: 下载器名称，None 表示全部
    :param btrfs_fsid_dedup: 当前请求的 Btrfs 文件系统去重配置
    :return: 原仪表盘的全局字节统计与本地目录可用容量
    """
    return sample_downloader(name, btrfs_fsid_dedup=btrfs_fsid_dedup)


@router.get("/statistic", summary="媒体数量统计", response_model=_SchemaStatistic)
def statistic(
    name: Optional[str] = None,
    service: DashboardQueryService = Depends(get_dashboard_query_service),
    _: Any = Depends(get_current_active_superuser),
) -> Any:
    """
    查询媒体数量统计信息
    """
    return service.statistic(name)


@router.get(
    "/statistic2", summary="媒体数量统计（API_TOKEN）", response_model=_SchemaStatistic
)
def statistic2(
    _: Annotated[str, Depends(verify_apitoken)],
    service: DashboardQueryService = Depends(get_dashboard_query_service),
) -> Any:
    """
    查询媒体数量统计信息 API_TOKEN认证（?token=xxx）
    """
    return service.statistic()


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
def processes(_: Any = Depends(get_current_active_superuser), page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
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
    name: Optional[str] = None,
    runtime_config: ApiRuntimeConfig = Depends(get_api_runtime_config),
    _: Any = Depends(get_current_active_superuser),
) -> Any:
    """
    查询下载器信息
    """
    runtime_config = resolve_api_runtime_config(runtime_config)
    return _build_downloader(
        name,
        btrfs_fsid_dedup=runtime_config.btrfs_fsid_dedup,
    )


@router.get(
    "/downloader2",
    summary="下载器信息（API_TOKEN）",
    response_model=_SchemaDownloaderInfo,
)
def downloader2(
    _: Annotated[str, Depends(verify_apitoken)],
    runtime_config: ApiRuntimeConfig = Depends(get_api_runtime_config),
) -> Any:
    """
    查询下载器信息 API_TOKEN认证（?token=xxx）
    """
    runtime_config = resolve_api_runtime_config(runtime_config)
    return _build_downloader(
        btrfs_fsid_dedup=runtime_config.btrfs_fsid_dedup,
    )


@router.get("/schedule", summary="后台服务", response_model=List[_SchemaScheduleInfo])
async def schedule(_: Any = Depends(get_current_active_superuser), page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    查询后台服务信息
    """
    # 同步 list() 内含同步进度读取，放到线程池执行避免阻塞事件循环
    return await run_in_threadpool(get_scheduler().list)


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
    # 异步进度后端读取，避免同步 Redis 调用阻塞事件循环
    progress = await get_scheduler().aget_progress(job_id)
    if not progress:
        return _SchemaResponse(success=False, message="后台服务不存在")
    return _SchemaResponse(success=True, data=progress.model_dump())


@router.get(
    "/schedule2",
    summary="后台服务（API_TOKEN）",
    response_model=List[_SchemaScheduleInfo],
)
async def schedule2(_: Annotated[str, Depends(verify_apitoken)], page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    查询下载器信息 API_TOKEN认证（?token=xxx）
    """
    # 同步 list() 内含同步进度读取，放到线程池执行避免阻塞事件循环
    return await run_in_threadpool(get_scheduler().list)


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
    # 异步进度后端读取，避免同步 Redis 调用阻塞事件循环
    progress = await get_scheduler().aget_progress(job_id)
    if not progress:
        return _SchemaResponse(success=False, message="后台服务不存在")
    return _SchemaResponse(success=True, data=progress.model_dump())


@router.get("/transfer", summary="文件整理统计", response_model=List[int])
async def transfer(
    days: Optional[int] = 7,
    service: DashboardQueryService = Depends(get_dashboard_query_service),
    _: Any = Depends(get_current_active_superuser),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    查询文件整理统计信息
    """
    return await service.transfer(days or 7)


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
def network(_: Any = Depends(get_current_active_superuser), page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    获取当前网络流量（上行和下行流量，单位：bytes/s）
    """
    return SystemUtils.network_usage()


@router.get(
    "/network2", summary="获取当前网络流量（API_TOKEN）", response_model=List[int]
)
def network2(_: Annotated[str, Depends(verify_apitoken)], page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    获取当前网络流量 API_TOKEN认证（?token=xxx）
    """
    return SystemUtils.network_usage()
