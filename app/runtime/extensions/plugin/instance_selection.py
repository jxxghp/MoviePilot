"""插件实例的调用目标解析。

一个插件按配置扇出多个实例后，外部调用若不指明走哪一个，只有两种可能：要么用户
事先指定过一个默认调用目标，要么这次调用无法确定目标。本模块只实现这两种，不提供
第三种「随便挑一个」——按登记顺序取首个、或在默认实例停用时改走另一个启用实例，
都会让调用悄悄落到用户没有选择的实例上，且不留任何痕迹。默认调用目标停用等同于
没有默认调用目标，一律报错。

实例状态落在插件实例配置表，本层不得反向依赖 DB 层，因此读取走可注入的钩子。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Optional

from app.runtime.extensions.contract.instance import (
    DEFAULT_INSTANCE_ID,
    describe_instance_candidates,
    instance_key,
    normalize_instance_id,
)


@dataclass(frozen=True)
class PluginInstanceTarget:
    """一个插件实例在调用目标解析中被看见的全部状态。

    ``is_default_target`` 是用户选定的默认调用目标，与实例标识是否为默认实例、
    是否跟随默认实例的版本都无关。
    """

    # 实例标识
    instance_id: str
    # 该实例是否启用
    is_enabled: bool
    # 该实例是否为本插件的默认调用目标
    is_default_target: bool


PluginInstanceTargetLister = Callable[[str], Sequence[PluginInstanceTarget]]


def _no_instance_targets(_plugin_id: str) -> Sequence[PluginInstanceTarget]:
    """实例状态读取尚未装配时报告该插件没有任何实例。"""
    return ()


_instance_target_lister: PluginInstanceTargetLister = _no_instance_targets


def configure_plugin_instance_targets(lister: PluginInstanceTargetLister) -> None:
    """由启动组合根注入插件实例状态的读取钩子。

    :param lister: 按插件标识返回该插件全部实例状态的函数
    :return: 无返回值
    """
    global _instance_target_lister
    _instance_target_lister = lister


def _ordered(targets: Iterable[PluginInstanceTarget]) -> list[PluginInstanceTarget]:
    """把实例状态按默认实例优先、其余按标识升序排列，使报错文案稳定可预期。

    :param targets: 实例状态集合
    :return: 排序后的实例状态列表
    """
    return sorted(
        targets,
        key=lambda target: (target.instance_id != DEFAULT_INSTANCE_ID, target.instance_id),
    )


def _describe_candidates(targets: Iterable[PluginInstanceTarget]) -> str:
    """列出可供显式指定的实例名及其启用状态。

    :param targets: 实例状态集合
    :return: 形如 ``default（已启用）、alt（已停用）`` 的描述，一个实例都没有时为「无」
    """
    return describe_instance_candidates(
        (target.instance_id, target.is_enabled) for target in _ordered(targets)
    )


def select_plugin_instance_id(
        plugin_id: str,
        targets: Iterable[PluginInstanceTarget],
        instance_id: Optional[str] = None,
) -> str:
    """在给定的实例状态集合中确定本次调用的实例标识。

    :param plugin_id: 插件标识
    :param targets: 该插件全部实例的状态
    :param instance_id: 调用方显式指定的实例标识，为空时按默认调用目标解析
    :return: 本次调用应当走的实例标识
    :raises ValueError: 显式指定的实例标识包含实例键分隔符
    :raises LookupError: 未指定实例，且没有已启用的默认调用目标
    """
    if instance_id:
        return normalize_instance_id(instance_id)

    collected = list(targets)
    default_target = next(
        (target for target in collected if target.is_default_target), None
    )
    if default_target is not None and default_target.is_enabled:
        return default_target.instance_id

    candidates = _describe_candidates(collected)
    if default_target is not None:
        raise LookupError(
            f"插件 {plugin_id} 的默认实例 {default_target.instance_id} 已停用，"
            f"调用必须显式指定实例；可选实例：{candidates}"
        )
    raise LookupError(
        f"插件 {plugin_id} 未设置默认实例，调用必须显式指定实例；可选实例：{candidates}"
    )


def resolve_plugin_instance_key(plugin_id: str, instance_id: Optional[str] = None) -> str:
    """确定本次调用的插件实例键。

    :param plugin_id: 插件标识
    :param instance_id: 调用方显式指定的实例标识，为空时按默认调用目标解析
    :return: 实例键，默认实例为裸插件标识
    :raises ValueError: 显式指定的实例标识包含实例键分隔符
    :raises LookupError: 未指定实例，且没有已启用的默认调用目标
    """
    resolved = select_plugin_instance_id(
        plugin_id, _instance_target_lister(plugin_id), instance_id
    )
    return instance_key(plugin_id, resolved)
