"""插件仪表盘声明的契约校验。"""

from __future__ import annotations

from typing import Any, Optional

from app.runtime.extensions.declaration import (
    declaration_config_component,
    declaration_config_form,
    declaration_dashboard_identity,
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

    :param declaration: `DashboardDeclaration` 实例，或插件直接交出的描述字典
    :param render_mode: 声明该仪表盘的扩展当前的渲染模式；为 None 时跳过
        ``config_component`` 与渲染模式的一致性校验
    :return: 违反契约的描述；声明合规时为 None
    """
    try:
        _, name = declaration_dashboard_identity(declaration)
        config_form = declaration_config_form(declaration)
        config_component = declaration_config_component(declaration)
    except Exception as error:
        return f"读取仪表盘声明出错：{error}"
    if not name:
        return "未声明非空的仪表盘展示名称 name"
    if config_form is not None and config_component:
        return "config_form 与 config_component 不可同时声明，配置界面二选一"
    if config_form is not None:
        return _config_form_violation(config_form)
    if config_component:
        return _config_component_violation(render_mode)
    return None


def _config_form_violation(config_form: Any) -> Optional[str]:
    """
    校验声明附带的 vuetify 配置界面是否是合法形状

    :param config_form: 声明的 config_form 字段，调用方已保证非 None
    :return: 违反契约的描述；形状合法时为 None
    """
    if not isinstance(config_form, (tuple, list)) or len(config_form) != 2:
        return "config_form 必须是（组件树, 默认数据）二元组"
    layout, defaults = config_form
    if not isinstance(layout, list):
        return f"config_form 的组件树必须是 list，实际是 {type(layout).__name__}"
    if not isinstance(defaults, dict):
        return f"config_form 的默认数据必须是 dict，实际是 {type(defaults).__name__}"
    return None


def _config_component_violation(render_mode: Optional[str]) -> Optional[str]:
    """
    校验 vue 模式配置组件声明是否与扩展渲染模式一致

    :param render_mode: 声明方扩展当前的渲染模式；为 None 时跳过校验
    :return: 违反契约的描述；渲染模式为 vue 或未知时为 None
    """
    if render_mode is not None and render_mode != "vue":
        return f"config_component 要求扩展渲染模式为 vue，实际是 {render_mode!r}"
    return None
