import fnmatch
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException
from starlette.responses import FileResponse, Response

from app.api.dependencies.auth import (
    get_current_active_manage_user,
    get_current_active_superuser,
    get_current_active_user,
)
from app.api.principal import ApiPrincipal
from app.api.response import (
    COLLECTION_PAGINATION_OPENAPI_KEY,
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
)
from app.application.configuration import get_api_runtime_config_snapshot
from app.application.directory import DirectoryHelper
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.transfer.facade import TransferChain
from app.foundation import text as text_tools
from app.runtime.progress import ProgressHelper
from app.schemas.common import ManageRequest as _SchemaManageRequest
from app.schemas.response import Response as _SchemaResponse
from app.schemas.system import TransferDirectoryConf as _SchemaTransferDirectoryConf
from app.schemas.types import ProgressKey
from app.schemas.workflow import FileItem as _SchemaFileItem

router = ResponseAPIRouter()


@router.get(
    "/directories",
    summary="查询目录配置",
    response_model=_SchemaResponse[list[_SchemaTransferDirectoryConf]],
)
def directory_settings(
    directory_type: str = "all",
    storage_type: str = "all",
    name: Optional[str] = None,
    _: ApiPrincipal = Depends(get_current_active_superuser),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> _SchemaResponse[Any]:
    """按用途、存储类型和名称筛选目录配置。"""
    helper = DirectoryHelper()
    if directory_type == "download":
        directories = helper.get_download_dirs()
    elif directory_type == "library":
        directories = helper.get_library_dirs()
    elif directory_type == "all":
        directories = helper.get_dirs()
    else:
        return _SchemaResponse(success=False, message="directory_type 无效")
    results = []
    for directory in directories:
        if storage_type == "local":
            if directory_type == "download" and directory.storage != "local":
                continue
            if directory_type == "library" and directory.library_storage != "local":
                continue
        elif storage_type == "remote":
            if directory_type == "download" and directory.storage == "local":
                continue
            if directory_type == "library" and directory.library_storage == "local":
                continue
        elif storage_type != "all":
            return _SchemaResponse(success=False, message="storage_type 无效")
        if name and name.lower() not in (directory.name or "").lower():
            continue
        results.append(
            {
                "name": directory.name,
                "priority": directory.priority,
                "storage": directory.storage,
                "download_path": directory.download_path,
                "library_path": directory.library_path,
                "library_storage": directory.library_storage,
                "media_type": directory.media_type,
                "media_category": directory.media_category,
                "media_category_id": directory.media_category_id,
                "monitor_type": directory.monitor_type,
                "monitor_mode": directory.monitor_mode,
                "transfer_type": directory.transfer_type,
                "overwrite_mode": directory.overwrite_mode,
                "renaming": directory.renaming,
                "scraping": directory.scraping,
                "notify": directory.notify,
            }
        )
    return _SchemaResponse(success=True, data=results)


@router.post("/manage", summary="网盘存储统一管理", response_model=_SchemaResponse[Dict[str, Any]])  # type: ignore[misc]
def manage(request: _SchemaManageRequest, _: ApiPrincipal = Depends(get_current_active_superuser)) -> Any:
    """
    网盘存储统一管理入口

    端点层不定义任何存储特定的名称与参数，
    存储标识、管理动作与表单参数由前端上送并原样透传给存储模块
    """
    result = StorageChain().manage_storage(
        storage=request.target,
        action=request.action,
        **request.params,
    )
    return _SchemaResponse(
        success=bool(result.get("success")),
        message=result.get("message"),
        data=result.get("data"),
    )


@router.post(
    "/list",
    summary="所有目录和文件",
    response_model=List[_SchemaFileItem],
    openapi_extra={COLLECTION_PAGINATION_OPENAPI_KEY: True},
)
def list_files(
    fileitem: _SchemaFileItem,
    sort: Optional[str] = "updated_at",
    keyword: Optional[str] = None,
    _: ApiPrincipal = Depends(get_current_active_manage_user),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    查询当前目录下所有目录和文件
    :param fileitem: 文件项
    :param sort: 排序方式，name:按名称排序，time:按修改时间排序
    :param keyword: 通配符过滤，支持 * 和 ?，如 *.mkv、movie?.*
    :param _: token
    :return: 所有目录和文件
    """
    return _list_files(fileitem=fileitem, sort=sort, keyword=keyword)


def _list_files(
    *,
    fileitem: _SchemaFileItem,
    sort: Optional[str],
    keyword: Optional[str],
) -> List[_SchemaFileItem]:
    """执行目录查询、通配符过滤和稳定排序，供管理端与 Agent 安全入口复用。"""
    file_list = StorageChain().list_files(fileitem)
    if file_list:
        if keyword:
            _pat = re.compile(fnmatch.translate(keyword), re.IGNORECASE)
            file_list = [f for f in file_list if _pat.match(f.name or "")]
        if sort == "name":
            file_list.sort(key=lambda x: text_tools.natural_sort_key(x.name or ""))
        else:
            file_list.sort(key=lambda x: x.modify_time or -math.inf, reverse=True)
    return file_list or []


@router.post(  # type: ignore[misc]
    "/agent/list",
    summary="查询 Agent 可用目录和文件",
    response_model=List[_SchemaFileItem],
    openapi_extra={COLLECTION_PAGINATION_OPENAPI_KEY: True},
)
def list_agent_files(
    fileitem: _SchemaFileItem,
    sort: Optional[str] = "updated_at",
    keyword: Optional[str] = None,
    _: Any = Depends(get_current_active_user),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> List[_SchemaFileItem]:
    """保留旧 Agent 普通用户目录读取能力，不开放创建、改名或删除入口。"""
    return _list_files(fileitem=fileitem, sort=sort, keyword=keyword)


@router.post("/mkdir", summary="创建目录", response_model=_SchemaResponse[None])
def mkdir(
    fileitem: _SchemaFileItem,
    name: str,
    _: ApiPrincipal = Depends(get_current_active_manage_user),
) -> Any:
    """
    创建目录
    :param fileitem: 文件项
    :param name: 目录名称
    :param _: token
    """
    if not name:
        return _SchemaResponse(success=False)
    result = StorageChain().create_folder(fileitem, name)
    if result:
        return _SchemaResponse(success=True)
    return _SchemaResponse(success=False)


@router.post("/delete", summary="删除文件或目录", response_model=_SchemaResponse[None])
def delete(fileitem: _SchemaFileItem, _: ApiPrincipal = Depends(get_current_active_manage_user)) -> Any:
    """
    删除文件或目录
    :param fileitem: 文件项
    :param _: token
    """
    result = StorageChain().delete_file(fileitem)
    if result:
        return _SchemaResponse(success=True)
    return _SchemaResponse(success=False)


@router.post(
    "/download",
    summary="下载文件",
    response_model=None,
    response_class=FileResponse,
    responses={
        200: {
            "description": "文件内容",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        },
        404: {"model": _SchemaResponse[None], "description": "文件下载失败"},
    },
)
def download(fileitem: _SchemaFileItem, _: ApiPrincipal = Depends(get_current_active_manage_user)) -> Any:
    """
    下载文件或目录
    :param fileitem: 文件项
    :param _: token
    """
    # 临时目录
    tmp_file = StorageChain().download_file(fileitem)
    if tmp_file:
        return FileResponse(path=tmp_file)
    return _SchemaResponse(success=False)


@router.post(
    "/image",
    summary="预览图片",
    response_model=None,
    response_class=Response,
    responses={
        200: {
            "description": "图片内容",
            "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
def image(fileitem: _SchemaFileItem, _: ApiPrincipal = Depends(get_current_active_manage_user)) -> Any:
    """
    下载文件或目录
    :param fileitem: 文件项
    :param _: token
    """
    # 临时目录
    tmp_file = StorageChain().download_file(fileitem)
    if not tmp_file:
        raise HTTPException(status_code=500, detail="图片读取出错")
    return Response(content=tmp_file.read_bytes(), media_type="image/jpeg")


@router.post("/rename", summary="重命名文件或目录", response_model=_SchemaResponse[None])
def rename(
    fileitem: _SchemaFileItem,
    new_name: str,
    recursive: Optional[bool] = False,
    _: ApiPrincipal = Depends(get_current_active_manage_user),
) -> Any:
    """
    重命名文件或目录
    :param fileitem: 文件项
    :param new_name: 新名称
    :param recursive: 是否递归修改
    :param _: token
    """
    if not new_name:
        return _SchemaResponse(success=False, message="新名称为空")

    # 重命名目录内文件
    if recursive:
        transferchain = TransferChain()
        runtime_config = get_api_runtime_config_snapshot()
        media_exts = (
            runtime_config.media_extensions + runtime_config.subtitle_extensions + runtime_config.audio_extensions
        )
        # 递归修改目录内文件（智能识别命名）
        sub_files: List[_SchemaFileItem] = StorageChain().list_files(fileitem)
        if sub_files:
            # 开始进度
            progress = ProgressHelper(ProgressKey.BatchRename)
            progress.start()
            total = len(sub_files)
            handled = 0
            for sub_file in sub_files:
                handled += 1
                progress.update(value=handled / total * 100, text=f"正在处理 {sub_file.name} ...")
                if sub_file.type == "dir":
                    continue
                if not sub_file.extension:
                    continue
                if f".{sub_file.extension.lower()}" not in media_exts:
                    continue
                sub_path = Path(f"{fileitem.path}{sub_file.name}")
                context = MediaChain().recognize_by_path(
                    sub_path,
                    obtain_images=False,
                )
                if not context or not context.media_info:
                    progress.end()
                    return _SchemaResponse(success=False, message=f"{sub_path.name} 未识别到媒体信息")
                new_path = transferchain.recommend_name(meta=context.meta_info, mediainfo=context.media_info)
                if not new_path:
                    progress.end()
                    return _SchemaResponse(success=False, message=f"{sub_path.name} 未识别到新名称")
                ret: _SchemaResponse[Any] = rename(
                    fileitem=sub_file,
                    new_name=Path(new_path).name,
                    recursive=False,
                )
                if not ret.success:
                    progress.end()
                    return _SchemaResponse(success=False, message=f"{sub_path.name} 重命名失败！")
            progress.end()
    # 重命名自己
    result = StorageChain().rename_file(fileitem, new_name)
    if result:
        return _SchemaResponse(success=True)
    return _SchemaResponse(success=False)
