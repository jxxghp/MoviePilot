from typing import Annotated, Any, List, Optional

from fastapi import APIRouter, Depends, Header

from app import schemas
from app.command import Command
from app.core.config import settings
from app.core.plugin import PluginManager
from app.core.security import verify_apikey, verify_token
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import get_current_active_superuser
from app.factory import app
from app.helper.plugin import PluginHelper
from app.log import logger
from app.scheduler import Scheduler
from app.schemas.types import SystemConfigKey

PROTECTED_ROUTES = {"/api/v1/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

PLUGIN_PREFIX = f"{settings.API_V1_STR}/plugin"

router = APIRouter()


def register_plugin_api(plugin_id: Optional[str] = None):
    """
    动态注册插件 API
    :param plugin_id: 插件 ID，如果为 None，则注册所有插件
    """
    _update_plugin_api_routes(plugin_id, action="add")


def remove_plugin_api(plugin_id: str):
    """
    动态移除单个插件的 API
    :param plugin_id: 插件 ID
    """
    _update_plugin_api_routes(plugin_id, action="remove")


def _update_plugin_api_routes(plugin_id: Optional[str], action: str):
    """
    插件 API 路由注册和移除
    :param plugin_id: 插件 ID，如果 action 为 "add" 且 plugin_id 为 None，则处理所有插件
                      如果 action 为 "remove"，plugin_id 必须是有效的插件 ID
    :param action: "add" 或 "remove"，决定是添加还是移除路由
    """
    if action not in {"add", "remove"}:
        raise ValueError("Action must be 'add' or 'remove'")

    is_modified = False
    existing_paths = {route.path: route for route in app.routes}

    plugin_ids = [plugin_id] if plugin_id else PluginManager().get_running_plugin_ids()
    for plugin_id in plugin_ids:
        routes_removed = _remove_routes(plugin_id)
        if routes_removed:
            is_modified = True

        if action != "add":
            continue
        # 获取插件的 API 路由信息
        plugin_apis = PluginManager().get_plugin_apis(plugin_id)
        for api in plugin_apis:
            api_path = f"{PLUGIN_PREFIX}{api.get('path', '')}"
            try:
                api["path"] = api_path
                allow_anonymous = api.pop("allow_anonymous", False)
                dependencies = api.setdefault("dependencies", [])
                if not allow_anonymous and Depends(verify_apikey) not in dependencies:
                    dependencies.append(Depends(verify_apikey))
                app.add_api_route(**api, tags=["plugin"])
                is_modified = True
                logger.debug(f"Added plugin route: {api_path}")
            except Exception as e:
                logger.error(f"Error adding plugin route {api_path}: {str(e)}")

    if is_modified:
        _clean_protected_routes(existing_paths)
        app.openapi_schema = None
        app.setup()


def _remove_routes(plugin_id: str) -> bool:
    """
    移除与单个插件相关的路由
    :param plugin_id: 插件 ID
    :return: 是否有路由被移除
    """
    if not plugin_id:
        return False
    prefix = f"{PLUGIN_PREFIX}/{plugin_id}/"
    routes_to_remove = [route for route in app.routes if route.path.startswith(prefix)]
    removed = False
    for route in routes_to_remove:
        try:
            app.routes.remove(route)
            removed = True
            logger.debug(f"Removed plugin route: {route.path}")
        except Exception as e:
            logger.error(f"Error removing plugin route {route.path}: {str(e)}")
    return removed


def _clean_protected_routes(existing_paths: dict):
    """
    清理受保护的路由，防止在插件操作中被删除或重复添加
    :param existing_paths: 当前应用的路由路径映射
    """
    for protected_route in PROTECTED_ROUTES:
        try:
            existing_route = existing_paths.get(protected_route)
            if existing_route:
                app.routes.remove(existing_route)
        except Exception as e:
            logger.error(f"Error removing protected route {protected_route}: {str(e)}")


@router.get("/", summary="所有插件", response_model=List[schemas.Plugin])
def all_plugins(_: schemas.TokenPayload = Depends(get_current_active_superuser),
                state: Optional[str] = "all") -> List[schemas.Plugin]:
    """
    查询所有插件清单，包括本地插件和在线插件，插件状态：installed, market, all
    """
    # 本地插件
    local_plugins = PluginManager().get_local_plugins()
    # 已安装插件
    installed_plugins = [plugin for plugin in local_plugins if plugin.installed]
    # 未安装的本地插件
    not_installed_plugins = [plugin for plugin in local_plugins if not plugin.installed]
    if state == "installed":
        return installed_plugins

    # 在线插件
    online_plugins = PluginManager().get_online_plugins()
    if not online_plugins:
        # 没有获取在线插件
        if state == "market":
            # 返回未安装的本地插件
            return not_installed_plugins
        return local_plugins

    # 插件市场插件清单
    market_plugins = []
    # 已安装插件IDS
    _installed_ids = [plugin.id for plugin in installed_plugins]
    # 未安装的线上插件或者有更新的插件
    for plugin in online_plugins:
        if plugin.id not in _installed_ids:
            market_plugins.append(plugin)
        elif plugin.has_update:
            market_plugins.append(plugin)
    # 未安装的本地插件，且不在线上插件中
    _plugin_ids = [plugin.id for plugin in market_plugins]
    for plugin in not_installed_plugins:
        if plugin.id not in _plugin_ids:
            market_plugins.append(plugin)
    # 返回插件清单
    if state == "market":
        # 返回未安装的插件
        return market_plugins
    # 返回所有插件
    return installed_plugins + market_plugins


@router.get("/installed", summary="已安装插件", response_model=List[str])
def installed(_: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    查询用户已安装插件清单
    """
    return SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []


@router.get("/statistic", summary="插件安装统计", response_model=dict)
def statistic(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    插件安装统计
    """
    return PluginHelper().get_statistic()


@router.get("/install/{plugin_id}", summary="安装插件", response_model=schemas.Response)
def install(plugin_id: str,
            repo_url: Optional[str] = "",
            force: Optional[bool] = False,
            _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    安装插件
    """
    # 已安装插件
    install_plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
    # 首先检查插件是否已经存在，并且是否强制安装，否则只进行安装统计
    if not force and plugin_id in PluginManager().get_plugin_ids():
        PluginHelper().install_reg(pid=plugin_id)
    else:
        # 插件不存在或需要强制安装，下载安装并注册插件
        if repo_url:
            state, msg = PluginHelper().install(pid=plugin_id, repo_url=repo_url)
            # 安装失败则直接响应
            if not state:
                return schemas.Response(success=False, message=msg)
        else:
            # repo_url 为空时，也直接响应
            return schemas.Response(success=False, message="没有传入仓库地址，无法正确安装插件，请检查配置")
    # 安装插件
    if plugin_id not in install_plugins:
        install_plugins.append(plugin_id)
        # 保存设置
        SystemConfigOper().set(SystemConfigKey.UserInstalledPlugins, install_plugins)
    # 加载插件到内存
    PluginManager().reload_plugin(plugin_id)
    # 注册插件服务
    Scheduler().update_plugin_job(plugin_id)
    # 注册菜单命令
    Command().init_commands(plugin_id)
    # 注册插件API
    register_plugin_api(plugin_id)
    return schemas.Response(success=True)


@router.get("/form/{plugin_id}", summary="获取插件表单页面")
def plugin_form(plugin_id: str,
                _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> dict:
    """
    根据插件ID获取插件配置表单
    """
    conf, model = PluginManager().get_plugin_form(plugin_id)
    return {
        "conf": conf,
        "model": model
    }


@router.get("/page/{plugin_id}", summary="获取插件数据页面")
def plugin_page(plugin_id: str, _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> List[dict]:
    """
    根据插件ID获取插件数据页面
    """
    return PluginManager().get_plugin_page(plugin_id)


@router.get("/dashboard/meta", summary="获取所有插件仪表板元信息")
def plugin_dashboard_meta(_: schemas.TokenPayload = Depends(verify_token)) -> List[dict]:
    """
    获取所有插件仪表板元信息
    """
    return PluginManager().get_plugin_dashboard_meta()


@router.get("/dashboard/{plugin_id}", summary="获取插件仪表板配置")
def plugin_dashboard(plugin_id: str, user_agent: Annotated[str | None, Header()] = None,
                     _: schemas.TokenPayload = Depends(verify_token)) -> schemas.PluginDashboard:
    """
    根据插件ID获取插件仪表板
    """
    return PluginManager().get_plugin_dashboard(plugin_id, user_agent=user_agent)


@router.get("/dashboard/{plugin_id}/{key}", summary="获取插件仪表板配置")
def plugin_dashboard(plugin_id: str, key: str, user_agent: Annotated[str | None, Header()] = None,
                     _: schemas.TokenPayload = Depends(verify_token)) -> schemas.PluginDashboard:
    """
    根据插件ID获取插件仪表板
    """
    return PluginManager().get_plugin_dashboard(plugin_id, key=key, user_agent=user_agent)


@router.get("/reset/{plugin_id}", summary="重置插件配置及数据", response_model=schemas.Response)
def reset_plugin(plugin_id: str,
                 _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    根据插件ID重置插件配置及数据
    """
    # 删除配置
    PluginManager().delete_plugin_config(plugin_id)
    # 删除插件所有数据
    PluginManager().delete_plugin_data(plugin_id)
    # 重新生效插件
    PluginManager().reload_plugin(plugin_id)
    # 注册插件服务
    Scheduler().update_plugin_job(plugin_id)
    # 注册菜单命令
    Command().init_commands(plugin_id)
    # 注册插件API
    register_plugin_api(plugin_id)
    return schemas.Response(success=True)


@router.get("/{plugin_id}", summary="获取插件配置")
def plugin_config(plugin_id: str,
                  _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> dict:
    """
    根据插件ID获取插件配置信息
    """
    return PluginManager().get_plugin_config(plugin_id)


@router.put("/{plugin_id}", summary="更新插件配置", response_model=schemas.Response)
def set_plugin_config(plugin_id: str, conf: dict,
                      _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    更新插件配置
    """
    # 保存配置
    PluginManager().save_plugin_config(plugin_id, conf)
    # 重新生效插件
    PluginManager().init_plugin(plugin_id, conf)
    # 注册插件服务
    Scheduler().update_plugin_job(plugin_id)
    # 注册菜单命令
    Command().init_commands(plugin_id)
    # 注册插件API
    register_plugin_api(plugin_id)
    return schemas.Response(success=True)


@router.delete("/{plugin_id}", summary="卸载插件", response_model=schemas.Response)
def uninstall_plugin(plugin_id: str,
                     _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    卸载插件
    """
    # 删除已安装信息
    install_plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
    for plugin in install_plugins:
        if plugin == plugin_id:
            install_plugins.remove(plugin)
            break
    # 保存
    SystemConfigOper().set(SystemConfigKey.UserInstalledPlugins, install_plugins)
    # 移除插件API
    remove_plugin_api(plugin_id)
    # 移除插件服务
    Scheduler().remove_plugin_job(plugin_id)
    # 移除插件
    PluginManager().remove_plugin(plugin_id)
    return schemas.Response(success=True)


# 注册全部插件API
register_plugin_api()
