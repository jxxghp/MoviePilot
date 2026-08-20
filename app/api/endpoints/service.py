from typing import Any

from fastapi import Depends, HTTPException
from starlette import status

from app.api.deps import get_current_active_superuser
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.runtime.extensions.declaration import SERVICE_INSTANCE_CONFIG_KEYS
from app.runtime.extensions.service_instance_registry import service_instance_registry
from app.schemas.service import ServiceConfigForm as _SchemaServiceConfigForm

router = ResponseAPIRouter()


@router.get(
    "/config_form/{config_key}/{service_type}",
    summary="获取服务实例类型的专属配置界面",
    response_model=_SchemaServiceConfigForm,
)
def config_form(
    config_key: str,
    service_type: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    按服务配置键与类型标识获取扩展为该类型声明的配置界面

    下载器、媒体服务器与消息通知共用本端点：三者的服务实例登记在同一张表里，
    按「配置键加类型标识」两个维度索引，界面形状也完全相同，拆成三个端点只会
    得到三份同样的代码。界面归属声明该类型的扩展本身，不归属某个插件：同一
    插件可能同时声明服务实例与另一种能力，此处只按类型索引到登记时随声明附带
    的界面，不会读到扩展自身的 get_form()。

    未登记的类型返回 ``available`` 为 False 而非报错。与存储端点不同，服务实例
    的内建类型登记在各内建模块的 capability.toml 里而不在本表中，本端点没有可用
    的全量类型目录，无法区分「类型不存在」与「类型没有专属界面」；把二者一并
    答成「没有专属界面」是此处唯一诚实的回答，前端据此沿用内建渲染方式。
    :param config_key: 服务配置键，取值为 Downloaders、MediaServers、Notifications
    :param service_type: 类型标识，即该族配置模型的 type
    :param _: 鉴权
    :return: available 为 False 时该类型没有专属界面，其余字段均为 None
    """
    if config_key not in SERVICE_INSTANCE_CONFIG_KEYS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"服务配置键 {config_key} 不支持声明服务实例",
        )
    empty = {
        "available": False,
        "name": None,
        "conf": None,
        "model": None,
        "component": None,
        "remote": None,
    }
    entry = service_instance_registry.find(config_key, service_type)
    if entry is None:
        return empty
    empty["name"] = entry.name
    if entry.config_form is not None:
        layout, defaults = entry.config_form
        return {**empty, "available": True, "conf": layout, "model": defaults}
    if entry.config_component is not None:
        return {
            **empty,
            "available": True,
            "component": entry.config_component.get("component"),
            "remote": entry.config_component.get("remote"),
        }
    return empty
