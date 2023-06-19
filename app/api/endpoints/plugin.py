from typing import Any

from fastapi import APIRouter, Depends

from app import schemas
from app.core.plugin import PluginManager
from app.db.models.user import User
from app.db.userauth import get_current_active_user

router = APIRouter()


@router.get("/", summary="运行插件方法", response_model=schemas.Response)
@router.post("/")
async def run_plugin_method(plugin_id: str, method: str,
                            _: User = Depends(get_current_active_user),
                            *args,
                            **kwargs) -> Any:
    """
    运行插件方法
    """
    return PluginManager().run_plugin_method(pid=plugin_id,
                                             method=method,
                                             *args,
                                             **kwargs)

# 注册插件API
for api in PluginManager().get_plugin_apis():
    router.add_api_route(**api)
