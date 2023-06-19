from typing import Any, List

from fastapi import APIRouter, Depends

from app import schemas
from app.core.plugin import PluginManager
from app.db.models.user import User
from app.db.systemconfig_oper import SystemConfigOper
from app.db.userauth import get_current_active_user
from app.schemas.types import SystemConfigKey

router = APIRouter()


@router.get("/", summary="所有插件", response_model=List[schemas.Plugin])
async def all_plugins(_: User = Depends(get_current_active_user)) -> Any:
    """
    查询所有插件清单
    """
    return PluginManager().get_plugin_apps()


@router.get("/installed", summary="已安装插件", response_model=List[str])
async def installed_plugins(_: User = Depends(get_current_active_user)) -> Any:
    """
    查询用户已安装插件清单
    """
    return SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []


# 注册插件API
for api in PluginManager().get_plugin_apis():
    router.add_api_route(**api)
