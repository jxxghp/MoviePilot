"""插件分身的创建接口。

这些路由并入 ``plugin`` 路由器，路径与单文件时期完全一致；单独成篇只是让分身创建与
插件目录、市场、静态资源等关注点各自分开，与 ``pluginfolder``/``pluginloglevel``/
``plugintarget`` 的切分方式一致。
"""

from typing import Any

from fastapi import Depends

from app.api.dependencies.auth import get_current_active_superuser
from app.api.endpoints.plugin import register_plugin
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.plugin.folders import add_clone_to_plugin_folder
from app.application.plugin.runtime import get_plugin_manager
from app.runtime.log import logger
from app.schemas.plugin import PluginCloneRequest as _SchemaPluginCloneRequest
from app.schemas.response import Response as _SchemaResponse

router = ResponseAPIRouter()


@router.post("/clone/{plugin_id}", summary="创建插件分身", response_model=_SchemaResponse[None])
def clone_plugin(
    plugin_id: str,
    clone_data: _SchemaPluginCloneRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    创建插件分身
    """
    plugin_manager = get_plugin_manager()
    try:
        with plugin_manager.mutation(f"创建插件 {plugin_id} 分身"):
            success, message = plugin_manager.clone_plugin(
                plugin_id=plugin_id,
                suffix=clone_data.suffix,
                name=clone_data.name,
                description=clone_data.description,
                version=clone_data.version,
                icon=clone_data.icon,
            )

            if success:
                # 分身服务已完成运行态加载，此处只补齐宿主注册。
                register_plugin(message)
                # 将分身插件添加到原插件所在的文件夹中
                add_clone_to_plugin_folder(plugin_id, message)
                return _SchemaResponse(success=True, message="插件分身创建成功")
            return _SchemaResponse(success=False, message=message)
    except Exception as e:
        logger.error(f"创建插件分身失败：{str(e)}")
        return _SchemaResponse(success=False, message=f"创建插件分身失败：{str(e)}")

