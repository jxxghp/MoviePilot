"""插件默认调用目标裁决与置位／清除测试。"""

from __future__ import annotations

import pytest

from app.runtime.extensions.plugin.target import PluginDefaultTargetControl
from app.schemas.plugin import PluginInstance


class _Harness:
    """组装 PluginDefaultTargetControl 依赖并记录调用轨迹的测试脚手架。"""

    def __init__(
        self,
        *,
        host_instance: PluginInstance | None = None,
        clones: list[PluginInstance] | None = None,
        plugin_exists: bool = True,
        running_ids: set[str] | None = None,
        set_result: bool = True,
    ) -> None:
        self.host_instance = host_instance
        self.clones = list(clones or [])
        self.plugin_exists_flag = plugin_exists
        self.running_ids = set(running_ids or set())
        self.saved_hosts: list[PluginInstance] = []
        self.set_calls: list[tuple[str, str]] = []
        self.clear_calls: list[str] = []
        self._set_result = set_result

    def _get_instance(self, instance_id: str) -> PluginInstance | None:
        for clone in self.clones:
            if clone.instance_id == instance_id:
                return clone
        return None

    def _instances_for_source(self, source_plugin_id: str) -> list[PluginInstance]:
        return [clone for clone in self.clones if clone.source_plugin_id == source_plugin_id]

    def _get_host_instance(self, plugin_id: str) -> PluginInstance | None:
        if self.host_instance is not None and self.host_instance.instance_id == plugin_id:
            return self.host_instance
        return None

    def _save_host_instance(self, instance: PluginInstance) -> None:
        self.saved_hosts.append(instance)
        self.host_instance = instance

    def _running(self) -> dict[str, object]:
        return {instance_id: object() for instance_id in self.running_ids}

    def _set_default_target(self, plugin_id: str, instance_id: str) -> bool:
        self.set_calls.append((plugin_id, instance_id))
        return self._set_result

    def _clear_default_target(self, plugin_id: str) -> None:
        self.clear_calls.append(plugin_id)

    def build(self) -> PluginDefaultTargetControl:
        """构造挂接本脚手架全部端口的裁决与置位控制器。"""
        return PluginDefaultTargetControl(
            plugin_exists=lambda _plugin_id: self.plugin_exists_flag,
            get_instance=self._get_instance,
            instances_for_source=self._instances_for_source,
            get_host_instance=self._get_host_instance,
            save_host_instance=self._save_host_instance,
            running=self._running,
            set_default_target=self._set_default_target,
            clear_default_target=self._clear_default_target,
        )


def _clone(instance_id: str, source_plugin_id: str, *, default: bool = False) -> PluginInstance:
    """构造一个分身实例描述。"""
    return PluginInstance(
        instance_id=instance_id,
        source_plugin_id=source_plugin_id,
        mode="virtual",
        is_default_target=default,
    )


def _host(plugin_id: str, *, default: bool = False) -> PluginInstance:
    """构造一个本体实例描述。"""
    return PluginInstance(
        instance_id=plugin_id,
        source_plugin_id=plugin_id,
        mode="host",
        is_default_target=default,
    )


# --------------------------------------------------------------------------- #
# resolve()：单实例场景与显式实例直通
# --------------------------------------------------------------------------- #


def test_resolve_returns_plugin_id_when_no_clones_exist():
    """只有本体、没有任何分身时直接返回插件 ID，不要求设置默认目标。"""
    harness = _Harness(host_instance=_host("PluginA"), running_ids={"PluginA"})

    assert harness.build().resolve("PluginA") == "PluginA"


def test_resolve_returns_argument_unchanged_when_it_is_already_a_clone_id():
    """传入的标识本身就是某个分身的实例 ID 时原样返回，该分身没有自己的下级分身。"""
    harness = _Harness(
        host_instance=_host("PluginA"),
        clones=[_clone("PluginAx2", "PluginA")],
        running_ids={"PluginAx2"},
    )

    assert harness.build().resolve("PluginAx2") == "PluginAx2"


# --------------------------------------------------------------------------- #
# resolve()：有分身时的默认目标裁决
# --------------------------------------------------------------------------- #


def test_resolve_uses_enabled_default_target_among_clones():
    """已有分身且默认目标已启用时，未指定实例的调用落到该默认目标。"""
    harness = _Harness(
        host_instance=_host("PluginA"),
        clones=[
            _clone("PluginAx2", "PluginA", default=True),
            _clone("PluginAx3", "PluginA"),
        ],
        running_ids={"PluginAx2", "PluginAx3"},
    )

    assert harness.build().resolve("PluginA") == "PluginAx2"


def test_resolve_host_itself_can_be_default_target():
    """本体同样可以被选为默认调用目标，与分身地位相同。"""
    harness = _Harness(
        host_instance=_host("PluginA", default=True),
        clones=[_clone("PluginAx2", "PluginA")],
        running_ids={"PluginA", "PluginAx2"},
    )

    assert harness.build().resolve("PluginA") == "PluginA"


def test_resolve_raises_when_no_default_target_set():
    """已有分身但未设置默认目标时报错，且不得回退到任何一个候选。"""
    harness = _Harness(
        host_instance=_host("PluginA"),
        clones=[_clone("PluginAx2", "PluginA"), _clone("PluginAx3", "PluginA")],
        running_ids={"PluginA", "PluginAx2", "PluginAx3"},
    )

    with pytest.raises(LookupError) as excinfo:
        harness.build().resolve("PluginA")

    message = str(excinfo.value)
    assert "PluginA" in message
    assert "未设置默认实例" in message
    assert "PluginA（已启用）" in message
    assert "PluginAx2（已启用）" in message
    assert "PluginAx3（已启用）" in message


def test_resolve_raises_when_default_target_disabled_and_does_not_fall_back():
    """默认目标已停用时必须报错，不得静默改走另一个正在运行的实例。"""
    harness = _Harness(
        host_instance=_host("PluginA"),
        clones=[
            _clone("PluginAx2", "PluginA", default=True),
            _clone("PluginAx3", "PluginA"),
        ],
        running_ids={"PluginAx3"},
    )

    with pytest.raises(LookupError) as excinfo:
        harness.build().resolve("PluginA")

    message = str(excinfo.value)
    assert "默认实例 PluginAx2 已停用" in message
    assert "PluginAx3（已启用）" in message


def test_resolve_candidate_description_orders_alphabetically():
    """候选实例描述按实例 ID 升序排列，报错文案稳定可预期。"""
    harness = _Harness(
        host_instance=_host("PluginZ"),
        clones=[_clone("PluginZb", "PluginZ"), _clone("PluginZa", "PluginZ")],
        running_ids=set(),
    )

    with pytest.raises(LookupError) as excinfo:
        harness.build().resolve("PluginZ")

    assert str(excinfo.value).endswith(
        "可选实例：PluginZ（已停用）、PluginZa（已停用）、PluginZb（已停用）"
    )


def test_resolve_treats_never_persisted_host_as_a_candidate():
    """本体从未被显式绑定过任何设置时，仍以默认视图参与候选而不是被略去。"""
    harness = _Harness(
        host_instance=None,
        clones=[_clone("PluginAx2", "PluginA")],
        running_ids={"PluginA"},
    )

    with pytest.raises(LookupError) as excinfo:
        harness.build().resolve("PluginA")

    assert "PluginA（已启用）" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# set_target()
# --------------------------------------------------------------------------- #


def test_set_target_upserts_never_persisted_host_before_setting():
    """本体从未落盘过时，设为默认目标前先落盘一条默认视图记录。"""
    harness = _Harness(host_instance=None)

    result = harness.build().set_target("PluginA", "PluginA")

    assert result is True
    assert len(harness.saved_hosts) == 1
    assert harness.saved_hosts[0].instance_id == "PluginA"
    assert harness.set_calls == [("PluginA", "PluginA")]


def test_set_target_does_not_resave_already_persisted_host():
    """本体已经落盘过时不重复保存，只转发置位调用。"""
    harness = _Harness(host_instance=_host("PluginA"))

    harness.build().set_target("PluginA", "PluginA")

    assert harness.saved_hosts == []
    assert harness.set_calls == [("PluginA", "PluginA")]


def test_set_target_delegates_clone_to_atomic_callable():
    """目标是已归属该插件的分身时，直接转发给原子置位端口。"""
    harness = _Harness(
        host_instance=_host("PluginA"),
        clones=[_clone("PluginAx2", "PluginA")],
    )

    result = harness.build().set_target("PluginA", "PluginAx2")

    assert result is True
    assert harness.set_calls == [("PluginA", "PluginAx2")]


def test_set_target_rejects_instance_not_belonging_to_plugin():
    """目标实例不存在或归属另一个插件时拒绝，且不下发到原子置位端口。"""
    harness = _Harness(
        host_instance=_host("PluginA"),
        clones=[_clone("PluginBx2", "PluginB")],
    )

    result = harness.build().set_target("PluginA", "PluginBx2")

    assert result is False
    assert harness.set_calls == []


def test_set_target_propagates_atomic_callable_failure():
    """原子置位端口报告目标不存在时如实透传，不伪装成功。"""
    harness = _Harness(
        host_instance=_host("PluginA"),
        clones=[_clone("PluginAx2", "PluginA")],
        set_result=False,
    )

    assert harness.build().set_target("PluginA", "PluginAx2") is False


def test_set_target_raises_when_plugin_missing():
    """插件本身不存在时拒绝设置默认目标。"""
    harness = _Harness(plugin_exists=False)

    with pytest.raises(LookupError):
        harness.build().set_target("Missing", "Missing")


# --------------------------------------------------------------------------- #
# clear_target()
# --------------------------------------------------------------------------- #


def test_clear_target_clears_when_matching_current_default():
    """请求清除的实例正是当前默认目标时才真正清除。"""
    harness = _Harness(
        host_instance=_host("PluginA"),
        clones=[_clone("PluginAx2", "PluginA", default=True)],
    )

    harness.build().clear_target("PluginA", "PluginAx2")

    assert harness.clear_calls == ["PluginA"]


def test_clear_target_is_noop_when_nothing_is_set():
    """插件当前没有任何默认目标置位时按空操作处理。"""
    harness = _Harness(
        host_instance=_host("PluginA"),
        clones=[_clone("PluginAx2", "PluginA")],
    )

    harness.build().clear_target("PluginA", "PluginAx2")

    assert harness.clear_calls == []


def test_clear_target_does_not_touch_a_different_current_default():
    """请求清除的实例并非当前默认目标时，不得误清另一个实例的置位。"""
    harness = _Harness(
        host_instance=_host("PluginA"),
        clones=[
            _clone("PluginAx2", "PluginA", default=True),
            _clone("PluginAx3", "PluginA"),
        ],
    )

    harness.build().clear_target("PluginA", "PluginAx3")

    assert harness.clear_calls == []


def test_clear_target_raises_when_plugin_missing():
    """插件本身不存在时拒绝清除默认目标。"""
    harness = _Harness(plugin_exists=False)

    with pytest.raises(LookupError):
        harness.build().clear_target("Missing", "Missing")
