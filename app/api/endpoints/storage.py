"""
提供存储配置及文件系统浏览、查询和操作接口。
"""

import fnmatch
import math
import posixpath
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
from app.application.storage import StorageHelper
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.transfer.facade import TransferChain
from app.foundation import text as text_tools
from app.runtime.localization import LocaleHelper
from app.runtime.progress import ProgressHelper
from app.schemas.common import ManageRequest as _SchemaManageRequest
from app.schemas.file import FileItem as _SchemaFileItem
from app.schemas.response import Response as _SchemaResponse
from app.schemas.storage import StorageCatalogOption as _SchemaStorageCatalogOption
from app.schemas.storage import StorageOption as _SchemaStorageOption
from app.schemas.system import TransferDirectoryConf as _SchemaTransferDirectoryConf
from app.schemas.types import ProgressKey, StorageSchema

router = ResponseAPIRouter()

_STORAGE_CATALOG_META = {
    StorageSchema.Local.value: ("本地", "mdi-folder-multiple-outline", False),
    StorageSchema.Alipan.value: ("阿里云盘", "mdi-cloud-outline", True),
    StorageSchema.U115.value: ("115网盘", "mdi-cloud-outline", True),
    StorageSchema.Rclone.value: ("RClone", "mdi-server-network-outline", True),
    StorageSchema.Alist.value: ("OpenList", "mdi-server-network-outline", True),
    StorageSchema.AlistGo.value: ("AList", "mdi-server-network-outline", True),
    StorageSchema.SMB.value: ("SMB网络共享", "mdi-folder-network-outline", True),
}


@router.get(
    "/directories",
    summary="查询目录配置",
    response_model=_SchemaResponse[list[_SchemaTransferDirectoryConf]],
)
def directory_settings(
    directory_type: str = "all",
    storage_type: str = "all",
    name: Optional[str] = None,
    _: ApiPrincipal = Depends(get_current_active_manage_user),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> _SchemaResponse[Any]:
    """
    按用途、存储类型和名称筛选目录配置。
    """
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
                "download_type_folder": directory.download_type_folder,
                "download_category_folder": directory.download_category_folder,
                "monitor_type": directory.monitor_type,
                "monitor_mode": directory.monitor_mode,
                "transfer_type": directory.transfer_type,
                "overwrite_mode": directory.overwrite_mode,
                "renaming": directory.renaming,
                "scraping": directory.scraping,
                "notify": directory.notify,
                "library_type_folder": directory.library_type_folder,
                "library_category_folder": directory.library_category_folder,
            }
        )
    return _SchemaResponse(success=True, data=results)


@router.get(
    "/options", summary="查询可用存储选项", response_model=List[_SchemaStorageOption]
)
def storage_options(
    _: ApiPrincipal = Depends(get_current_active_user),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> List[_SchemaStorageOption]:
    """
    返回不包含连接配置和凭据的存储名称与类型。
    """
    return [
        _SchemaStorageOption(name=storage.name or storage.type or "", type=storage.type or "")
        for storage in StorageHelper.get_storagies()
        if storage.type
    ]


@router.get(
    "/catalog",
    summary="查询存储类型目录",
    response_model=List[_SchemaStorageCatalogOption],
)
def storage_catalog(
    _: ApiPrincipal = Depends(get_current_active_user),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> List[_SchemaStorageCatalogOption]:
    """
    返回所有可新增存储类型及其展示元数据，不包含连接配置和凭据。
    """
    return [
        _SchemaStorageCatalogOption(
            type=storage_type,
            name=default_name,
            name_i18n=LocaleHelper.translate(
                f"storage.{storage_type}",
                default=default_name,
            ),
            icon=icon,
            remote=remote,
        )
        for storage_type, (default_name, icon, remote) in _STORAGE_CATALOG_META.items()
    ]


@router.post("/manage", summary="网盘存储统一管理", response_model=_SchemaResponse[Dict[str, Any]])
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
        message=result.get("message") or "",
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
    :param _: 依赖注入的已登录管理身份，仅用于权限校验
    :return: 所有目录和文件
    """
    fileitem = _ensure_manage_storage_item(fileitem, _)
    return _list_files(fileitem=fileitem, sort=sort, keyword=keyword)


def _list_files(
    *,
    fileitem: _SchemaFileItem,
    sort: Optional[str],
    keyword: Optional[str],
) -> List[_SchemaFileItem]:
    """
    执行目录查询、通配符过滤和稳定排序，供管理端与 Agent 安全入口复用。
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
    return file_list or []


def _split_agent_storage_path(value: Optional[str], default_storage: str) -> tuple[str, str]:
    """
    拆分 Agent 文件浏览使用的存储标识和路径。

    Agent 端点不应把用户提交的路径直接交给存储提供方；这里先统一
    ``storage:/path`` 与 ``/path`` 两种表示，后续只和已配置目录根比较。
    """
    raw = str(value or "/").strip() or "/"
    storage = default_storage or StorageSchema.Local.value
    for storage_schema in StorageSchema:
        prefix = f"{storage_schema.value}:"
        if raw.startswith(prefix):
            storage = storage_schema.value
            raw = raw[len(prefix):] or "/"
            break
    return storage, raw


def _is_path_below(root: Path, target: Path) -> bool:
    """
    判断本地目标是否位于配置根目录下，包含根目录本身。
    """
    try:
        return target == root or target.is_relative_to(root)
    except ValueError:
        return False


def _is_agent_storage_path_allowed(fileitem: _SchemaFileItem) -> bool:
    """
    限制 Agent 文件浏览只能访问已配置的下载或媒体库目录。

    这是 Agent 的普通用户读取边界：管理端 ``/storage/list`` 仍由管理权限
    保护，但 Agent 不能借助 ``path=/`` 让本地存储列出容器任意目录。所有
    路径都按真实路径比较，以阻止通过符号链接逃逸本地配置根目录。
    """
    try:
        requested_storage, requested_raw_path = _split_agent_storage_path(
            fileitem.path,
            fileitem.storage or StorageSchema.Local.value,
        )
        configured_roots: list[tuple[str, str]] = []
        for directory in DirectoryHelper.get_dirs():
            for path_value, storage_value in (
                (directory.download_path, directory.storage),
                (directory.library_path, directory.library_storage),
            ):
                if not path_value:
                    continue
                root_storage, root_raw_path = _split_agent_storage_path(
                    path_value,
                    storage_value or StorageSchema.Local.value,
                )
                configured_roots.append((root_storage, root_raw_path))

        if requested_storage == StorageSchema.Local.value:
            requested_path = Path(requested_raw_path).expanduser()
            if not requested_path.is_absolute():
                return False
            target = requested_path.resolve(strict=False)
            for root_storage, root_raw_path in configured_roots:
                root = Path(root_raw_path).expanduser()
                if root_storage != StorageSchema.Local.value or not root.is_absolute():
                    continue
                if _is_path_below(root.resolve(strict=False), target):
                    return True
            return False

        requested_remote_path = posixpath.normpath(f"/{requested_raw_path.lstrip('/')}")
        for root_storage, root_raw_path in configured_roots:
            if root_storage != requested_storage:
                continue
            root_remote_path = posixpath.normpath(f"/{root_raw_path.lstrip('/')}")
            if requested_remote_path == root_remote_path or requested_remote_path.startswith(
                f"{root_remote_path.rstrip('/')}/"
            ):
                return True
        return False
    except (OSError, RuntimeError, TypeError, ValueError):
        # 解析失败时拒绝访问，避免异常路径退化为全盘浏览。
        return False


def _is_safe_storage_child_name(name: Optional[str]) -> bool:
    """
    校验文件管理操作使用的单级子名称，拒绝路径分隔符和特殊目录名。
    """
    value = str(name or "").strip()
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
    )


def _storage_child_item(fileitem: _SchemaFileItem, name: str) -> _SchemaFileItem:
    """
    按存储类型构造一个待校验的子路径文件项。
    """
    if (fileitem.storage or StorageSchema.Local.value) == StorageSchema.Local.value:
        child_path = (Path(fileitem.path or "/") / name).as_posix()
    else:
        child_path = posixpath.join(
            posixpath.normpath(f"/{(fileitem.path or '/').lstrip('/')}"),
            name,
        )
    return fileitem.model_copy(update={"path": child_path})


def _storage_sibling_item(fileitem: _SchemaFileItem, name: str) -> _SchemaFileItem:
    """
    按存储类型构造一个待校验的同级目标路径。

    重命名接口实际把目标放在源对象的父目录中，不能复用创建子目录
    使用的 ``当前路径/名称`` 计算方式。
    """
    if (fileitem.storage or StorageSchema.Local.value) == StorageSchema.Local.value:
        target_path = (Path(fileitem.path or "/").parent / name).as_posix()
    else:
        source_path = posixpath.normpath(f"/{(fileitem.path or '/').lstrip('/')}")
        target_path = posixpath.join(posixpath.dirname(source_path), name)
    return fileitem.model_copy(
        update={
            "path": target_path,
            "fileid": None,
            "parent_fileid": None,
        }
    )


def _storage_identity_matches(
    requested: _SchemaFileItem,
    actual: _SchemaFileItem,
) -> bool:
    """
    确认客户端提交的对象标识与按路径查询到的真实对象一致。

    ``path`` 由客户端提供，只能作为查询条件，不能单独作为授权凭据。
    远端存储的实际删除、重命名和目录浏览可能使用 ``fileid`` 或
    ``drive_id``，所以凡是客户端提交了这些标识，都必须与服务端回执
    完全一致；缺少服务端回执时拒绝继续操作。
    """
    if (requested.storage or StorageSchema.Local.value) != (
        actual.storage or StorageSchema.Local.value
    ):
        return False
    for field in ("fileid", "drive_id"):
        requested_value = getattr(requested, field, None)
        if requested_value is not None and requested_value != getattr(actual, field, None):
            return False
    return True


def _resolve_verified_storage_item(
    fileitem: _SchemaFileItem,
) -> Optional[_SchemaFileItem]:
    """
    按路径查询并核验存储对象，返回可直接交给存储提供方的服务端对象。

    这是 Agent 和普通管理用户的对象级授权边界：客户端路径只用于定位
    候选对象，底层实际操作必须使用同一个已核验的 ``FileItem``，避免
    合法路径搭配另一组远端文件 ID 或云盘空间标识。
    """
    if not _is_agent_storage_path_allowed(fileitem):
        return None
    try:
        actual = StorageChain().get_item(fileitem)
    except Exception:
        # 无法确认远端对象状态时必须拒绝，不能退回信任客户端字段。
        return None
    if not actual or not _storage_identity_matches(fileitem, actual):
        return None
    if not _is_agent_storage_path_allowed(actual):
        return None
    return actual


def _ensure_manage_storage_path(fileitem: _SchemaFileItem, current_user: Any) -> None:
    """
    将非超级管理员的文件管理范围限制到已配置的下载或媒体库目录。

    超级管理员保留文件管理器的完整能力；普通管理用户不能借助文件管理
    接口读取配置目录、删除任意容器路径或把重命名/建目录目标写到目录根外。
    """
    # 保留内部同步调用（例如批量重命名）不经过 FastAPI 注入时的兼容性；
    # HTTP 路由始终会收到真实的 ApiPrincipal。
    if not hasattr(current_user, "is_superuser") or current_user.is_superuser:
        return
    if not _is_agent_storage_path_allowed(fileitem):
        raise HTTPException(status_code=403, detail="管理用户只能操作已配置的存储目录")


def _ensure_manage_storage_item(
    fileitem: _SchemaFileItem,
    current_user: Any,
) -> _SchemaFileItem:
    """
    校验并返回非超级管理员实际可以操作的服务端对象。

    超级管理员沿用原有完整文件管理能力；其他管理用户必须使用按路径
    查询并完成身份绑定的对象，失败时拒绝访问。
    """
    if not hasattr(current_user, "is_superuser") or current_user.is_superuser:
        return fileitem
    actual = _resolve_verified_storage_item(fileitem)
    if not actual:
        raise HTTPException(status_code=403, detail="无法验证存储对象或对象不在允许范围内")
    return actual


@router.post(
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
    """
    只允许 Agent 浏览已配置的下载或媒体库目录，不开放创建、改名或删除入口。
    """
    verified_item = _resolve_verified_storage_item(fileitem)
    if not verified_item:
        raise HTTPException(status_code=403, detail="Agent 只能浏览已验证的存储目录")
    return _list_files(fileitem=verified_item, sort=sort, keyword=keyword)


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
    :param _: 依赖注入的已登录管理身份，仅用于权限校验
    """
    if not name:
        return _SchemaResponse(success=False)
    if not _is_safe_storage_child_name(name):
        return _SchemaResponse(success=False, message="目录名不能包含路径分隔符")
    fileitem = _ensure_manage_storage_item(fileitem, _)
    _ensure_manage_storage_path(_storage_child_item(fileitem, name), _)
    result = StorageChain().create_folder(fileitem, name)
    if result:
        return _SchemaResponse(success=True)
    return _SchemaResponse(success=False)


@router.post("/delete", summary="删除文件或目录", response_model=_SchemaResponse[None])
def delete(
    fileitem: _SchemaFileItem,
    _: ApiPrincipal = Depends(get_current_active_manage_user),
) -> Any:
    """
    删除文件或目录

    :param fileitem: 文件项
    :param _: 依赖注入的已登录管理身份，仅用于权限校验
    """
    fileitem = _ensure_manage_storage_item(fileitem, _)
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
def download(
    fileitem: _SchemaFileItem,
    _: ApiPrincipal = Depends(get_current_active_manage_user),
) -> Any:
    """
    下载文件或目录

    :param fileitem: 文件项
    :param _: 依赖注入的已登录管理身份，仅用于权限校验
    """
    fileitem = _ensure_manage_storage_item(fileitem, _)
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
def image(
    fileitem: _SchemaFileItem,
    _: ApiPrincipal = Depends(get_current_active_manage_user),
) -> Any:
    """
    读取存储中的图片并作为 JPEG 响应返回。

    :param fileitem: 文件项
    :param _: 依赖注入的已登录管理身份，仅用于权限校验
    """
    fileitem = _ensure_manage_storage_item(fileitem, _)
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
    :param _: 依赖注入的已登录管理身份，仅用于权限校验
    """
    if not new_name:
        return _SchemaResponse(success=False, message="新名称为空")
    if not _is_safe_storage_child_name(new_name):
        return _SchemaResponse(success=False, message="名称不能包含路径分隔符")
    fileitem = _ensure_manage_storage_item(fileitem, _)
    _ensure_manage_storage_path(_storage_sibling_item(fileitem, new_name), _)

    # 重命名目录内文件
    if recursive:
        transferchain = TransferChain()
        runtime_config = get_api_runtime_config_snapshot()
        media_exts = (
            runtime_config.media_extensions + runtime_config.subtitle_extensions + runtime_config.audio_extensions
        )
        # 递归修改目录内文件（智能识别命名）
        sub_files: List[_SchemaFileItem] = StorageChain().list_files(fileitem, recursion=True) or []
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
                sub_path = Path(sub_file.path or "")
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
                    _=_,
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
