"""插件登录认证提供方声明的契约校验。"""

from __future__ import annotations

from typing import Any, Optional

from app.runtime.extensions.declaration import (
    declaration_auth_provider_fields,
    declaration_config_component,
    declaration_config_form,
)
from app.runtime.extensions.plugin.config_interface import config_interface_violation


def auth_provider_declaration_violation(
    declaration: Any, *, render_mode: Optional[str] = None
) -> Optional[str]:
    """
    校验认证提供方声明是否满足登记契约

    契约要求声明是 `AuthProviderDeclaration` 实例，或插件直接交出的字段字典；
    配置界面二选一——``config_form``（vuetify 组件树加默认数据）与
    ``config_component``（vue 模式下从本扩展联邦远程加载的组件名）不可同时声明，
    给出 ``config_form`` 时形状须合法，给出 ``config_component`` 时声明方的渲染
    模式须为 vue。任一不满足都拒绝登记，不留到调用时才失败。

    :param declaration: `AuthProviderDeclaration` 实例，或插件直接交出的字段字典
    :param render_mode: 声明该认证提供方的扩展当前的渲染模式；为 None 时跳过
        ``config_component`` 与渲染模式的一致性校验
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        fields = declaration_auth_provider_fields(declaration)
        config_form = declaration_config_form(declaration)
        config_component = declaration_config_component(declaration)
    except Exception as error:
        return f"读取认证提供方声明出错：{error}"
    if fields is None:
        return f"{declaration!r} 既不是 AuthProviderDeclaration 也不是字段字典"
    return config_interface_violation(config_form, config_component, render_mode=render_mode)
