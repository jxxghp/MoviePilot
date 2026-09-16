"""插件文件夹增量管理端点。"""

from typing import Any

from fastapi import Depends

from app.api.dependencies.auth import get_current_active_superuser_async
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.plugin.folders import get_plugin_folder_service
from app.schemas.plugin import (
    PluginFolderPluginsUpdateRequest,
    PluginFoldersData,
    PluginFolderUpdateRequest,
)
from app.schemas.response import Response

router = ResponseAPIRouter()


@router.get(  # type: ignore[misc]
    "/folders",
    summary="获取插件文件夹配置",
    response_model=PluginFoldersData,
)
async def get_plugin_folders(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> dict[str, Any]:
    """获取插件文件夹分组配置。"""
    return get_plugin_folder_service().get_or_empty()


@router.post(  # type: ignore[misc]
    "/folders", summary="保存插件文件夹配置", response_model=Response[None]
)
async def save_plugin_folders(
    folders: PluginFoldersData,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """保存插件文件夹分组配置。"""
    result = await get_plugin_folder_service().save(folders.root)
    return Response(success=result.success, message=result.message)


@router.post(  # type: ignore[misc]
    "/folders/{folder_name}",
    summary="创建插件文件夹",
    response_model=Response[None],
)
async def create_plugin_folder(
    folder_name: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """创建新的插件文件夹。"""
    result = await get_plugin_folder_service().create(folder_name)
    return Response(success=result.success, message=result.message)


@router.delete(  # type: ignore[misc]
    "/folders/{folder_name}",
    summary="删除插件文件夹",
    response_model=Response[None],
)
async def delete_plugin_folder(
    folder_name: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """删除插件文件夹。"""
    result = await get_plugin_folder_service().delete(folder_name)
    return Response(success=result.success, message=result.message)


@router.patch(  # type: ignore[misc]
    "/folders/{folder_name}",
    summary="更新插件文件夹",
    response_model=Response[None],
)
async def update_plugin_folder(
    folder_name: str,
    folder: PluginFolderUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """增量更新插件文件夹名称或展示配置。"""
    changes = folder.model_dump(
        by_alias=True,
        exclude={"new_name"},
        exclude_unset=True,
    )
    result = await get_plugin_folder_service().update_folder(
        folder_name,
        new_name=folder.new_name,
        changes=changes,
    )
    return Response(success=result.success, message=result.message)


@router.put(  # type: ignore[misc]
    "/folders/{folder_name}/plugins",
    summary="更新文件夹中的插件",
    response_model=Response[None],
)
async def update_folder_plugins(
    folder_name: str,
    plugin_update: list[str] | PluginFolderPluginsUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """条件替换指定文件夹中的插件列表，并兼容旧数组请求。"""
    if isinstance(plugin_update, list):
        plugin_ids = plugin_update
        expected_plugin_ids = None
    else:
        plugin_ids = plugin_update.plugins
        expected_plugin_ids = plugin_update.expected_plugins
    result = await get_plugin_folder_service().update_plugins(
        folder_name,
        plugin_ids,
        expected_plugin_ids,
    )
    return Response(success=result.success, message=result.message)


@router.put(  # type: ignore[misc]
    "/folders/{folder_name}/plugins/{plugin_id}",
    summary="移动插件到文件夹",
    response_model=Response[None],
)
async def assign_plugin_to_folder(
    folder_name: str,
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """把一个插件原子迁移到目标文件夹。"""
    result = await get_plugin_folder_service().assign_plugin(folder_name, plugin_id)
    return Response(success=result.success, message=result.message)


@router.delete(  # type: ignore[misc]
    "/folders/{folder_name}/plugins/{plugin_id}",
    summary="从文件夹移除插件",
    response_model=Response[None],
)
async def remove_plugin_from_folder(
    folder_name: str,
    plugin_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """只从指定文件夹移除一个插件。"""
    result = await get_plugin_folder_service().remove_plugin_from_folder(
        folder_name,
        plugin_id,
    )
    return Response(success=result.success, message=result.message)
