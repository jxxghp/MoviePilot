from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header

from app import schemas
from app.core.plugin import PluginManager
from app.core.security import verify_token
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.plugin import PluginHelper
from app.scheduler import Scheduler
from app.schemas.types import SystemConfigKey

router = APIRouter()


def register_plugin_api(plugin_id: str = None):
    """
    注册插件API（先删除后新增）
    """
    for api in PluginManager().get_plugin_apis(plugin_id):
        for r in router.routes:
            if r.path == api.get("path"):
                router.routes.remove(r)
                break
        router.add_api_route(**api)


def remove_plugin_api(plugin_id: str):
    """
    移除插件API
    """
    for api in PluginManager().get_plugin_apis(plugin_id):
        for r in router.routes:
            if r.path == api.get("path"):
                router.routes.remove(r)
                break


@router.get("/", summary="所有插件", response_model=list[schemas.Plugin])
def all_plugins(_: schemas.TokenPayload = Depends(verify_token), state: str = "all") -> list[schemas.Plugin]:
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
        if plugin.id not in _installed_ids or plugin.has_update:
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


@router.get("/installed", summary="已安装插件", response_model=list[str])
def installed(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
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
            repo_url: str = "",
            force: bool = False,
            _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    安装插件
    """
    # 已安装插件
    install_plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
    # 如果是非本地括件，或者强制安装时，则需要下载安装
    if repo_url and (force or plugin_id not in PluginManager().get_plugin_ids()):
        # 下载安装
        state, msg = PluginHelper().install(pid=plugin_id, repo_url=repo_url)
        if not state:
            # 安装失败
            return schemas.Response(success=False, message=msg)
    # 安装插件
    if plugin_id not in install_plugins:
        install_plugins.append(plugin_id)
        # 保存设置
        SystemConfigOper().set(SystemConfigKey.UserInstalledPlugins, install_plugins)
    # 加载插件到内存
    PluginManager().reload_plugin(plugin_id)
    # 注册插件服务
    Scheduler().update_plugin_job(plugin_id)
    # 注册插件API
    register_plugin_api(plugin_id)
    return schemas.Response(success=True)


@router.get("/form/{plugin_id}", summary="获取插件表单页面")
def plugin_form(plugin_id: str,
                _: schemas.TokenPayload = Depends(verify_token)) -> dict:
    """
    根据插件ID获取插件配置表单
    """
    conf, model = PluginManager().get_plugin_form(plugin_id)
    return {
        "conf": conf,
        "model": model
    }


@router.get("/page/{plugin_id}", summary="获取插件数据页面")
def plugin_page(plugin_id: str, _: schemas.TokenPayload = Depends(verify_token)) -> list[dict]:
    """
    根据插件ID获取插件数据页面
    """
    return PluginManager().get_plugin_page(plugin_id)


@router.get("/dashboard/meta", summary="获取所有插件仪表板元信息")
def plugin_dashboard_meta(_: schemas.TokenPayload = Depends(verify_token)) -> list[dict]:
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
    return PluginManager().get_plugin_dashboard(plugin_id, key=None, user_agent=user_agent)


@router.get("/dashboard/{plugin_id}/{key}", summary="获取插件仪表板配置")
def plugin_dashboard(plugin_id: str, key: str, user_agent: Annotated[str | None, Header()] = None,
                     _: schemas.TokenPayload = Depends(verify_token)) -> schemas.PluginDashboard:
    """
    根据插件ID获取插件仪表板
    """
    return PluginManager().get_plugin_dashboard(plugin_id, key=key, user_agent=user_agent)


@router.get("/reset/{plugin_id}", summary="重置插件配置", response_model=schemas.Response)
def reset_plugin(plugin_id: str, _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据插件ID重置插件配置
    """
    # 删除配置
    PluginManager().delete_plugin_config(plugin_id)
    # 重新生效插件
    PluginManager().init_plugin(plugin_id, {
        "enabled": False,
        "enable": False
    })
    # 注册插件服务
    Scheduler().update_plugin_job(plugin_id)
    # 注册插件API
    register_plugin_api(plugin_id)
    return schemas.Response(success=True)


@router.get("/{plugin_id}", summary="获取插件配置")
def plugin_config(plugin_id: str, _: schemas.TokenPayload = Depends(verify_token)) -> dict:
    """
    根据插件ID获取插件配置信息
    """
    return PluginManager().get_plugin_config(plugin_id)


@router.put("/{plugin_id}", summary="更新插件配置", response_model=schemas.Response)
def set_plugin_config(plugin_id: str, conf: dict,
                      _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    更新插件配置
    """
    # 保存配置
    PluginManager().save_plugin_config(plugin_id, conf)
    # 重新生效插件
    PluginManager().init_plugin(plugin_id, conf)
    # 注册插件服务
    Scheduler().update_plugin_job(plugin_id)
    # 注册插件API
    register_plugin_api(plugin_id)
    return schemas.Response(success=True)


@router.delete("/{plugin_id}", summary="卸载插件", response_model=schemas.Response)
def uninstall_plugin(plugin_id: str,
                     _: schemas.TokenPayload = Depends(verify_token)) -> Any:
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
    # 移除插件
    PluginManager().remove_plugin(plugin_id)
    # 移除插件服务
    Scheduler().remove_plugin_job(plugin_id)
    # 移除插件API
    remove_plugin_api(plugin_id)
    return schemas.Response(success=True)


# 注册全部插件API
register_plugin_api()
