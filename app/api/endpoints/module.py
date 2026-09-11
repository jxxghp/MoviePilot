"""系统内置模块目录、开关和可用性测试接口。"""

from fastapi import Depends

from app.adapters.web.security.access import verify_token
from app.api.dependencies.auth import get_current_active_superuser_async
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.configuration import get_runtime_settings
from app.application.module import get_module_manager
from app.runtime.extensions.module.adapter import (
    capture_host_module_config,
    should_expose_host_module_option,
    should_run_host_module,
)
from app.runtime.localization import LocaleHelper
from app.schemas.response import Response as _SchemaResponse
from app.schemas.system import SystemModuleCatalogListData as _SchemaSystemModuleCatalogListData
from app.schemas.system import SystemModuleListData as _SchemaSystemModuleListData
from app.schemas.system import SystemModuleSettingListData as _SchemaSystemModuleSettingListData
from app.schemas.token import TokenPayload as _SchemaTokenPayload

router = ResponseAPIRouter()

_MODULE_DESCRIPTION_FALLBACKS = {
    "downloader": "下载器适配器，负责创建和管理下载任务。",
    "indexer": "站点索引模块，负责搜索站点资源并解析结果。",
    "mediarecognize": "媒体数据源，负责媒体识别、搜索或探索数据。",
    "mediaserver": "媒体服务器适配器，负责同步媒体库并执行媒体操作。",
    "notification": "消息通知通道，负责发送系统事件和任务结果。",
    "other": "媒体处理或运行基础模块。",
}


def _module_description(module_id: str, name: str, module_type: str) -> str:
    """读取模块的本地化职责说明，并为新增模块按类型提供可读兜底。"""
    fallback = _MODULE_DESCRIPTION_FALLBACKS.get(module_type, f"{name} 模块。")
    return LocaleHelper.translate(
        f"system.modules.{module_id}.description",
        default=fallback,
    )


@router.get(
    "/modulelist",
    summary="查询已启用的模块ID列表",
    response_model=_SchemaResponse[_SchemaSystemModuleListData],
)  # type: ignore[misc]
def modulelist(
    _: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[_SchemaSystemModuleListData]:
    """查询当前配置下应参与健康检查的模块 ID 列表。"""
    modules = []
    for spec in get_module_manager().list_enabled_specs():
        module_id = spec.id
        name = str(spec.metadata["name"])
        modules.append(
            {
                "id": module_id,
                "name": name,
                "name_i18n": LocaleHelper.translate(
                    f"system.modules.{module_id}.name",
                    default=name,
                ),
                "name_key": f"system.modules.{module_id}.name",
            }
        )
    return _SchemaResponse(success=True, data={"modules": modules})


@router.get(
    "/module-catalog",
    summary="查询宿主模块目录",
    response_model=_SchemaResponse[_SchemaSystemModuleCatalogListData],
)  # type: ignore[misc]
def module_catalog(
    _: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[_SchemaSystemModuleCatalogListData]:
    """返回模块及其服务类型目录，供前端选择器统一构造选项。"""
    manager = get_module_manager()
    specs = manager.list_specs()
    snapshot = capture_host_module_config(specs)
    modules = []
    for spec in specs:
        name = str(spec.metadata["name"])
        selector = spec.selector
        option_value = None
        if selector is not None and selector.kind == "system_config_item":
            option_value = str(selector.config["match_value"])
        modules.append(
            {
                "id": spec.id,
                "name": name,
                "name_i18n": LocaleHelper.translate(
                    f"system.modules.{spec.id}.name",
                    default=name,
                ),
                "name_key": f"system.modules.{spec.id}.name",
                "description_i18n": _module_description(
                    spec.id,
                    name,
                    str(spec.metadata.get("type", "")),
                ),
                "description_key": f"system.modules.{spec.id}.description",
                "type": str(spec.metadata["type"]),
                "subtype": str(spec.metadata["subtype"]),
                "option_value": option_value,
                "enabled": should_expose_host_module_option(spec, snapshot),
                "active": should_run_host_module(spec, snapshot),
            }
        )
    return _SchemaResponse(success=True, data={"modules": modules})


@router.get(
    "/module-settings",
    summary="查询可手动开关的内置模块",
    response_model=_SchemaResponse[_SchemaSystemModuleSettingListData],
)  # type: ignore[misc]
async def module_settings(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[_SchemaSystemModuleSettingListData]:
    """查询没有其它激活配置、可由用户统一控制的内置模块。"""
    configured = get_runtime_settings().get("MODULE_ENABLE", {})
    if not isinstance(configured, dict):
        configured = {}

    modules = []
    for spec in get_module_manager().list_switchable_specs():
        module_id = spec.id
        name = str(spec.metadata["name"])
        modules.append(
            {
                "id": module_id,
                "name": name,
                "name_i18n": LocaleHelper.translate(
                    f"system.modules.{module_id}.name",
                    default=name,
                ),
                "name_key": f"system.modules.{module_id}.name",
                "description_i18n": _module_description(
                    module_id,
                    name,
                    str(spec.metadata.get("type", "")),
                ),
                "description_key": f"system.modules.{module_id}.description",
                "enabled": configured.get(module_id, True) is not False,
            }
        )
    return _SchemaResponse(success=True, data={"modules": modules})


@router.get(  # type: ignore[misc]
    "/moduletest/{moduleid}",
    summary="模块可用性测试",
    response_model=_SchemaResponse[None],
)
def moduletest(
    moduleid: str,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[None]:
    """运行指定模块的内置可用性测试。"""
    state, errmsg = get_module_manager().test(moduleid)
    return _SchemaResponse(success=state, message=errmsg)
