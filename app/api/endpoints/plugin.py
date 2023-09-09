from typing import Any, List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.core.plugin import PluginManager
from app.core.security import verify_token
from app.db import get_db
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey

router = APIRouter()


@router.get("/", summary="所有插件", response_model=List[schemas.Plugin])
def all_plugins(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询所有插件清单
    """
    return PluginManager().get_plugin_apps()


@router.get("/installed", summary="已安装插件", response_model=List[str])
def installed_plugins(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询用户已安装插件清单
    """
    return SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []


@router.get("/install/{plugin_id}", summary="安装插件", response_model=schemas.Response)
def install_plugin(plugin_id: str,
                   _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    安装插件
    """
    # 已安装插件
    install_plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
    # 安装插件
    if plugin_id not in install_plugins:
        install_plugins.append(plugin_id)
        # 保存设置
        SystemConfigOper().set(SystemConfigKey.UserInstalledPlugins, install_plugins)
        # 重载插件管理器
        PluginManager().init_config()
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
def plugin_page(plugin_id: str, _: schemas.TokenPayload = Depends(verify_token)) -> List[dict]:
    """
    根据插件ID获取插件配置信息
    """
    return PluginManager().get_plugin_page(plugin_id)


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
    根据插件ID获取插件配置信息
    """
    # 保存配置
    PluginManager().save_plugin_config(plugin_id, conf)
    # 重新生效插件
    PluginManager().reload_plugin(plugin_id, conf)
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
    # 重载插件管理器
    PluginManager().init_config()
    return schemas.Response(success=True)


# 注册插件API
for api in PluginManager().get_plugin_apis():
    router.add_api_route(**api)
