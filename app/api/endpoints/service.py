from typing import Any

from fastapi import Depends, HTTPException
from starlette import status

from app.api.deps import get_current_active_superuser
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.runtime.extensions.service_family_registry import service_family_registry
from app.runtime.extensions.service_instance_registry import service_instance_registry
from app.schemas.service import ServiceConfigForm as _SchemaServiceConfigForm

router = ResponseAPIRouter()


@router.get(
    "/config_form/{capability}/{service_type}",
    summary="获取服务实例类型的专属配置界面",
    response_model=_SchemaServiceConfigForm,
)
def config_form(
    capability: str,
    service_type: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    按能力标签与类型标识获取扩展为该类型声明的配置界面

    下载器、媒体服务器与消息通知共用本端点：三者的服务实例登记在同一张表里，
    按「能力标签加类型标识」两个维度索引，界面形状也完全相同，拆成三个端点只会
    得到三份同样的代码。界面归属声明该类型的扩展本身，不归属某个插件：同一
    插件可能同时声明服务实例与另一种能力，此处只按类型索引到登记时随声明附带
    的界面，不会读到扩展自身的 get_form()。

    未登记的类型返回 ``available`` 为 False 而非报错。与存储端点不同，服务实例
    的内建类型登记在各内建模块的 capability.toml 里而不在本表中，本端点没有可用
    的全量类型目录，无法区分「类型不存在」与「类型没有专属界面」；把二者一并
    答成「没有专属界面」是此处唯一诚实的回答，前端据此沿用内建渲染方式。

    本端点同时下发 ``multi_instance``：前端要决定该类型的配置列表上要不要给出
    新增第二份的入口，而只有登记表知道这件事。未登记的类型答 True，与内建类型
    一律可配多份、以及声明缺省即多实例两处口径一致。

    ``config_schema`` 与 ``available`` 各自独立下发：契约描述配置形状，界面描述
    配置呈现，声明方可以只给其一。只声明契约的类型 ``available`` 仍为 False，
    前端据契约生成默认表单。
    :param capability: 能力标签，取值为宿主已登记的服务族，内建为 downloader、
        mediaserver、notification
    :param service_type: 类型标识，即该族配置模型的 type
    :param _: 鉴权
    :return: available 为 False 时该类型没有专属界面，界面相关字段均为 None
    """
    if not service_family_registry.is_registered(capability):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"服务能力 {capability} 不支持声明服务实例",
        )
    empty = {
        "available": False,
        "name": None,
        "multi_instance": True,
        "conf": None,
        "model": None,
        "component": None,
        "remote": None,
        "config_schema": None,
    }
    entry = service_instance_registry.find(capability, service_type)
    if entry is None:
        return empty
    empty["name"] = entry.name
    empty["multi_instance"] = entry.multi_instance
    empty["config_schema"] = entry.config_schema
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
