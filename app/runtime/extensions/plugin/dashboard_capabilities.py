"""插件仪表盘声明的契约校验。"""

from __future__ import annotations

from typing import Any, Optional

from app.runtime.extensions.declaration import (
    declaration_config_component,
    declaration_config_form,
    declaration_dashboard_identity,
    declaration_service_instance_requirement,
)
from app.runtime.extensions.plugin.config_interface import config_interface_violation
from app.runtime.extensions.service_instance_requirement import (
    service_instance_requirement_violation,
)


def dashboard_declaration_violation(
    declaration: Any, *, render_mode: Optional[str] = None
) -> Optional[str]:
    """
    校验仪表盘声明是否满足登记契约

    契约要求声明非空的展示名称 name；配置界面二选一——``config_form``（vuetify
    组件树加默认数据）与 ``config_component``（vue 模式下从本扩展联邦远程加载的
    组件名）不可同时声明，给出 ``config_form`` 时形状须合法，给出
    ``config_component`` 时声明方的渲染模式须为 vue。任一不满足都拒绝登记，
    不留到调用时才失败。key 不做非空校验，空字符串代表插件的默认仪表盘，与
    既有单仪表盘约定一致。

    声明了 requires_service_instance 时只判它的形状，不判该能力标签有没有登记成
    服务族；判据见 `app.runtime.extensions.service_instance_requirement`。

    :param declaration: `DashboardDeclaration` 实例，或插件直接交出的描述字典
    :param render_mode: 声明该仪表盘的扩展当前的渲染模式；为 None 时跳过
        ``config_component`` 与渲染模式的一致性校验
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        _, name = declaration_dashboard_identity(declaration)
        config_form = declaration_config_form(declaration)
        config_component = declaration_config_component(declaration)
        requirement = declaration_service_instance_requirement(declaration)
    except Exception as error:
        return f"读取仪表盘声明出错：{error}"
    if not name:
        return "未声明非空的仪表盘展示名称 name"
    interface_violation = config_interface_violation(
        config_form, config_component, render_mode=render_mode
    )
    if interface_violation:
        return interface_violation
    return service_instance_requirement_violation(requirement)
