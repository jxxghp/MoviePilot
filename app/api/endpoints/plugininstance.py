"""插件实例彻底清理接口。"""

from typing import Any

from fastapi import Depends

from app.api.dependencies.auth import get_current_active_superuser
from app.api.dependencies.plugin import get_plugin_config_command
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.plugin.config import PluginConfigCommand, PluginPurgeScope
from app.schemas.plugin import PluginInstancePurgeOutcome as _SchemaPluginInstancePurgeOutcome
from app.schemas.plugin import PluginInstancePurgeRequest as _SchemaPluginInstancePurgeRequest
from app.schemas.response import Response as _SchemaResponse

router = ResponseAPIRouter()


@router.post(  # type: ignore[misc]
    "/instance/{instance_id}/purge",
    summary="按选定范围彻底清理插件实例",
    response_model=_SchemaResponse[_SchemaPluginInstancePurgeOutcome],
)
def purge_plugin_instance(
    instance_id: str,
    request: _SchemaPluginInstancePurgeRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser),
    command: PluginConfigCommand = Depends(get_plugin_config_command),
) -> Any:
    """
    按用户逐项勾选的范围彻底清理一个实例的配置与数据

    与停用是两件事：停用只把启用位置假、设置原样留着且可随时恢复，这里才真正删除
    用户数据，因而范围完全由请求给定，一项没选直接拒绝。分身会连同实例行一并消失，
    本体的行则保留——它还承载着这个插件的启用状态与展示覆盖。

    权限与卸载、重置同档（仅超级管理员），因为破坏性相当：它删的是同一批东西。
    ``instance_id`` 会被拼进插件数据目录路径，用例层在动任何存储之前先做格式校验，
    删除目录时再按解析后的真实路径确认落在插件数据根内。
    """
    result = command.purge(
        instance_id,
        PluginPurgeScope(
            config=request.config,
            plugin_data=request.plugin_data,
            own_database=request.own_database,
            data_directory=request.data_directory,
        ),
    )
    if not result.success:
        return _SchemaResponse(success=False, message=result.message)
    return _SchemaResponse(
        success=True,
        data=_SchemaPluginInstancePurgeOutcome(
            purged=list(result.purged),
            instance_removed=result.instance_removed,
        ),
    )
