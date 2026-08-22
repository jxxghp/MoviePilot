import fnmatch
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException
from starlette import status
from starlette.responses import FileResponse, Response

from app.schemas.common import ManageRequest as _SchemaManageRequest
from app.schemas.response import Response as _SchemaResponse
from app.schemas.storage import StorageConfigForm as _SchemaStorageConfigForm
from app.schemas.workflow import FileItem as _SchemaFileItem
from app.api.response import ResponseAPIRouter
from app.application.orchestration.media import MediaChain
from app.application.orchestration.storage import StorageChain
from app.application.orchestration.transfer import TransferChain
from app.application.configuration import get_api_runtime_config_snapshot
from app.runtime.extensions.registry.storage import storage_backend_registry
from app.api.principal import ApiPrincipal
from app.api.dependencies.auth import (
    get_current_active_manage_user,
    get_current_active_superuser,
)
from app.runtime.progress import ProgressHelper
from app.schemas.types import ProgressKey
from app.foundation import text as text_tools

router = ResponseAPIRouter()


@router.get(
    "/config_form/{storage_type}",
    summary="获取存储类型的专属配置界面",
    response_model=_SchemaStorageConfigForm,
)
def config_form(
    storage_type: str, _: ApiPrincipal = Depends(get_current_active_superuser)
) -> Any:
    """
    按存储类型获取扩展为其声明的配置界面

    界面归属声明该类型的扩展本身，不归属某个插件：同一插件可能同时声明存储与
    另一种能力，此处只按存储类型索引到登记时随声明附带的界面，不会读到扩展
    自身的 get_form()。界面按扩展声明时的渲染模式二选一返回：vuetify 模式给
    conf/model 组件树，vue 模式给 component/remote 供前端从联邦远程加载组件。
    内建类型的界面已由前端内置、未随登记附带界面，``available`` 为 False 而非
    报错；只有存储类型本身未登记时才视为请求出错。
    :param storage_type: 存储标识，即 StorageConf.type
    :param _: 鉴权
    :return: available 为 False 时该类型没有专属界面，其余字段均为 None
    """
    entry = storage_backend_registry.find(storage_type)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"存储类型 {storage_type} 不存在",
        )
    empty = {"available": False, "conf": None, "model": None, "component": None, "remote": None}
    if entry.config_form is not None:
        layout, defaults = entry.config_form
        return {**empty, "available": True, "conf": layout, "model": defaults}
    if entry.config_component is not None:
        return {
            **empty,
            "available": True,
            "component": entry.config_component.get("component"),
            "remote": entry.config_component.get("remote"),
        }
    return empty


@router.post(
    "/manage", summary="网盘存储统一管理", response_model=_SchemaResponse[Dict[str, Any]]
)
def manage(
    request: _SchemaManageRequest, _: ApiPrincipal = Depends(get_current_active_superuser)
) -> Any:
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


@router.post("/list", summary="所有目录和文件", response_model=List[_SchemaFileItem])
def list_files(
    fileitem: _SchemaFileItem,
    sort: Optional[str] = "updated_at",
    keyword: Optional[str] = None,
    _: ApiPrincipal = Depends(get_current_active_manage_user),
) -> Any:
    """
    查询当前目录下所有目录和文件
    :param fileitem: 文件项
    :param sort: 排序方式，name:按名称排序，time:按修改时间排序
    :param keyword: 通配符过滤，支持 * 和 ?，如 *.mkv、movie?.*
    :param _: token
    :return: 所有目录和文件
    """
    file_list = StorageChain().list_files(fileitem)
    if file_list:
        if keyword:
            _pat = re.compile(fnmatch.translate(keyword), re.IGNORECASE)
            file_list = [f for f in file_list if _pat.match(f.name or "")]
        if sort == "name":
            file_list.sort(key=lambda x: text_tools.natural_sort_key(x.name or ""))
        else:
            file_list.sort(key=lambda x: x.modify_time or -math.inf, reverse=True)
    return file_list


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
def delete(
    fileitem: _SchemaFileItem, _: ApiPrincipal = Depends(get_current_active_manage_user)
) -> Any:
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
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
        404: {"model": _SchemaResponse[None], "description": "文件下载失败"},
    },
)
def download(
    fileitem: _SchemaFileItem, _: ApiPrincipal = Depends(get_current_active_manage_user)
) -> Any:
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
            "content": {
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
def image(
    fileitem: _SchemaFileItem, _: ApiPrincipal = Depends(get_current_active_manage_user)
) -> Any:
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
            runtime_config.media_extensions
            + runtime_config.subtitle_extensions
            + runtime_config.audio_extensions
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
                progress.update(
                    value=handled / total * 100, text=f"正在处理 {sub_file.name} ..."
                )
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
                    return _SchemaResponse(
                        success=False, message=f"{sub_path.name} 未识别到媒体信息"
                    )
                new_path = transferchain.recommend_name(
                    meta=context.meta_info, mediainfo=context.media_info
                )
                if not new_path:
                    progress.end()
                    return _SchemaResponse(
                        success=False, message=f"{sub_path.name} 未识别到新名称"
                    )
                ret: _SchemaResponse = rename(
                    fileitem=sub_file, new_name=Path(new_path).name, recursive=False
                )
                if not ret.success:
                    progress.end()
                    return _SchemaResponse(
                        success=False, message=f"{sub_path.name} 重命名失败！"
                    )
            progress.end()
    # 重命名自己
    result = StorageChain().rename_file(fileitem, new_name)
    if result:
        return _SchemaResponse(success=True)
    return _SchemaResponse(success=False)
