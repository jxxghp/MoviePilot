"""插件默认调用目标裁决：未指定实例的调用选择实例，以及默认目标的置位与清除。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

from app.schemas.plugin import PluginInstance

GetInstance = Callable[[str], Optional[PluginInstance]]
InstancesForSource = Callable[[str], list[PluginInstance]]
PluginExists = Callable[[str], bool]
RunningInstances = Callable[[], Mapping[str, Any]]
AtomicSetDefaultTarget = Callable[[str, str], bool]
ClearDefaultTarget = Callable[[str], None]


@dataclass(frozen=True)
class PluginCallCandidate:
    """一个插件实例在默认调用目标裁决中的可见状态。

    ``is_default_target`` 是用户选定的默认调用目标，与该实例当前是否在运行
    无关；``is_running`` 是该实例当前是否在运行，与调用目标的选定无关。
    """

    instance_id: str
    is_running: bool
    is_default_target: bool


def _ordered(candidates: list[PluginCallCandidate]) -> list[PluginCallCandidate]:
    """把候选实例按实例 ID 升序排列，使报错文案稳定可预期。"""
    return sorted(candidates, key=lambda candidate: candidate.instance_id)


def _describe(candidates: list[PluginCallCandidate]) -> str:
    """列出可供显式指定的实例名及其运行状态。

    :param candidates: 候选实例集合
    :return: 形如 ``PluginA（已启用）、PluginAx2（已停用）`` 的描述，候选为空时为「无」
    """
    if not candidates:
        return "无"
    return "、".join(
        f"{candidate.instance_id}（{'已启用' if candidate.is_running else '已停用'}）"
        for candidate in _ordered(candidates)
    )


class PluginDefaultTargetControl:
    """裁决插件未指定实例时的调用目标，并管理默认调用目标的置位与清除。

    一个插件按配置扇出多个实例后，「调用没指定实例」只允许两种结局：走用户
    选定且正在运行的默认调用目标，或者报错。绝不按登记顺序取第一个，也绝不
    在默认目标停用时静默改走另一个正在运行的实例——那等于用户停用了一个实例、
    调用却被悄悄改道，且不留任何痕迹。只有本体、没有任何分身的插件不受这套
    机制约束，直接使用本体，不要求显式设置默认目标，单实例场景不应被打扰。
    """

    def __init__(
        self,
        *,
        plugin_exists: PluginExists,
        get_instance: GetInstance,
        instances_for_source: InstancesForSource,
        get_host_instance: GetInstance,
        save_host_instance: Callable[[PluginInstance], None],
        running: RunningInstances,
        set_default_target: AtomicSetDefaultTarget,
        clear_default_target: ClearDefaultTarget,
    ) -> None:
        """保存本体与分身的实例持久化端口、运行态端口和默认目标置位的原子写入端口。"""
        self._plugin_exists = plugin_exists
        self._get_instance = get_instance
        self._instances_for_source = instances_for_source
        self._get_host_instance = get_host_instance
        self._save_host_instance = save_host_instance
        self._running = running
        self._set_default_target = set_default_target
        self._clear_default_target = clear_default_target

    @staticmethod
    def _default_host_instance(plugin_id: str) -> PluginInstance:
        """本体从未被显式绑定过版本、日志等级或默认目标时的默认视图。"""
        return PluginInstance(
            instance_id=plugin_id,
            source_plugin_id=plugin_id,
            mode="host",
            follow_current_version=True,
        )

    def _host_instance(self, plugin_id: str) -> PluginInstance:
        """读取源插件本体的实例描述，从未绑定过时给出默认视图。"""
        return self._get_host_instance(plugin_id) or self._default_host_instance(plugin_id)

    def _candidates(self, plugin_id: str) -> list[PluginCallCandidate]:
        """组装插件全部实例（含本体）在调用目标裁决中的可见状态。"""
        running = self._running()
        instances = [self._host_instance(plugin_id), *self._instances_for_source(plugin_id)]
        return [
            PluginCallCandidate(
                instance_id=instance.instance_id,
                is_running=instance.instance_id in running,
                is_default_target=instance.is_default_target,
            )
            for instance in instances
        ]

    def resolve(self, plugin_id: str) -> str:
        """确定按插件 ID 发起、未指定实例的调用应当落到哪个实例。

        插件从未被创建过分身时直接返回插件 ID 本身（即本体），不查默认目标
        置位——这也覆盖了调用方直接传入某个分身自身实例 ID 的情形：分身的
        实例 ID 不会作为任何插件的源插件 ID 拥有分身，因而同样原样返回。
        已有分身时必须命中已设置且正在运行的默认调用目标才会被采用。

        :param plugin_id: 插件 ID，也可以是调用方已经明确知道的具体实例 ID
        :return: 应当使用的实例 ID
        :raise LookupError: 已有分身但未设置默认调用目标，或默认调用目标已停用
        """
        if not self._instances_for_source(plugin_id):
            return plugin_id

        candidates = self._candidates(plugin_id)
        default = next(
            (candidate for candidate in candidates if candidate.is_default_target), None
        )
        if default is not None and default.is_running:
            return default.instance_id

        candidate_desc = _describe(candidates)
        if default is not None:
            raise LookupError(
                f"插件 {plugin_id} 的默认实例 {default.instance_id} 已停用，"
                f"调用必须显式指定实例；可选实例：{candidate_desc}"
            )
        raise LookupError(
            f"插件 {plugin_id} 未设置默认实例，调用必须显式指定实例；可选实例：{candidate_desc}"
        )

    def set_target(self, plugin_id: str, instance_id: str) -> bool:
        """把插件的默认调用目标改为指定实例，同一事务内清除同插件的旧置位。

        ``instance_id`` 等于插件 ID 时视为把本体设为默认目标；本体此前从未被
        显式绑定过任何设置时，先落盘一条默认视图的本体记录，确保随后的数据库
        级清旧置新有行可操作——这与版本切换、日志等级两处对本体的写入语义一致。

        :param plugin_id: 插件 ID
        :param instance_id: 要设为默认调用目标的实例 ID
        :return: 目标实例存在时为 True；指定的非本体实例不归属该插件时为 False
        :raise LookupError: 插件不存在
        """
        if not self._plugin_exists(plugin_id):
            raise LookupError(f"插件 {plugin_id} 不存在")
        if instance_id == plugin_id:
            if self._get_host_instance(plugin_id) is None:
                self._save_host_instance(self._default_host_instance(plugin_id))
        else:
            instance = self._get_instance(instance_id)
            if instance is None or instance.source_plugin_id != plugin_id:
                return False
        return self._set_default_target(plugin_id, instance_id)

    def clear_target(self, plugin_id: str, instance_id: str) -> None:
        """清除插件的默认调用目标置位，仅当当前置位的正是指定实例时才动作。

        请求清除的实例并非当前置位（含插件当前没有任何置位）时按空操作处理，
        这是清除接口的幂等语义，不是「找不到就报错」。

        :param plugin_id: 插件 ID
        :param instance_id: 请求清除默认调用目标的实例 ID
        :raise LookupError: 插件不存在
        """
        if not self._plugin_exists(plugin_id):
            raise LookupError(f"插件 {plugin_id} 不存在")
        current = next(
            (candidate for candidate in self._candidates(plugin_id) if candidate.is_default_target),
            None,
        )
        if current is None or current.instance_id != instance_id:
            return
        self._clear_default_target(plugin_id)
