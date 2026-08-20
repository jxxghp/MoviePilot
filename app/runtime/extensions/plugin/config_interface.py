"""声明附带配置界面的契约校验。

存储、认证提供方、仪表盘与服务实例族各自声明的配置界面共用同一套语义：
``config_form`` 是 vuetify 模式下的（组件树, 默认数据）二元组，``config_component``
是 vue 模式下从声明方联邦远程加载的组件名，两者互斥。校验规则集中在此，
各族的契约校验只负责本族特有的部分。

判据见 docs/plugin-extension-architecture.md 第 4 节。
"""

from __future__ import annotations

from typing import Any, Optional


def config_interface_violation(
    config_form: Any, config_component: Any, *, render_mode: Optional[str] = None
) -> Optional[str]:
    """
    校验声明附带的配置界面是否满足登记契约

    ``config_form`` 与 ``config_component`` 不可同时声明；给出 ``config_form``
    时形状须合法，给出 ``config_component`` 时声明方的渲染模式须为 vue。
    两者都不给出合法，表示该声明没有专属配置界面。

    :param config_form: 声明的 config_form 字段
    :param config_component: 声明的 config_component 字段
    :param render_mode: 声明方扩展当前的渲染模式；为 None 时跳过 ``config_component``
        与渲染模式的一致性校验
    :return: 违反契约的描述；配置界面合规时为 None
    """
    if config_form is not None and config_component:
        return "config_form 与 config_component 不可同时声明，配置界面二选一"
    if config_form is not None:
        return config_form_violation(config_form)
    if config_component:
        return config_component_violation(render_mode)
    return None


def config_form_violation(config_form: Any) -> Optional[str]:
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


def config_component_violation(render_mode: Optional[str]) -> Optional[str]:
    """
    校验 vue 模式配置组件声明是否与扩展渲染模式一致

    :param render_mode: 声明方扩展当前的渲染模式；为 None 时跳过校验
    :return: 违反契约的描述；渲染模式为 vue 或未知时为 None
    """
    if render_mode is not None and render_mode != "vue":
        return f"config_component 要求扩展渲染模式为 vue，实际是 {render_mode!r}"
    return None
