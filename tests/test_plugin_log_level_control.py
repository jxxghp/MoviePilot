"""插件实例日志等级总览的分身/本体身份校验测试。"""

from __future__ import annotations

import pytest

from app.runtime.extensions.plugin.loglevel import PluginLogLevelControl
from app.schemas.plugin import PluginInstance


def _control(*, instances: dict[str, PluginInstance]) -> PluginLogLevelControl:
    """构造仅需要的最小依赖，其余端口在这组用例中不会被调用。"""
    return PluginLogLevelControl(
        plugin_exists=lambda plugin_id: True,
        get_instance=instances.get,
        instances_for_source=lambda source_plugin_id: [
            instance
            for instance in instances.values()
            if instance.source_plugin_id == source_plugin_id
        ],
        get_host_instance=lambda plugin_id: None,
        read_log_level=lambda _instance_id: (None, None),
        write_log_level=lambda _instance_id, _level, _expires_at: None,
    )


def test_list_levels_rejects_clone_own_id_as_plugin_id():
    """用分身自身的实例 ID 查询日志等级总览必须拒绝，不能把分身伪装成本体。"""
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    control = _control(instances={"DemoPluginWork": clone})

    with pytest.raises(LookupError):
        control.list_levels("DemoPluginWork")


def test_set_level_rejects_clone_own_id_as_plugin_id():
    """写路径同样要拒绝分身自身实例 ID，否则会把该行改写成本体记录。"""
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    control = PluginLogLevelControl(
        plugin_exists=lambda plugin_id: True,
        get_instance={"DemoPluginWork": clone}.get,
        instances_for_source=lambda source_plugin_id: [],
        get_host_instance=lambda plugin_id: None,
        read_log_level=lambda _instance_id: (None, None),
        write_log_level=lambda _instance_id, _level, _expires_at: None,
    )

    with pytest.raises(LookupError):
        control.set_level("DemoPluginWork", "DemoPluginWork", "DEBUG")



def test_clear_level_rejects_clone_own_id_as_plugin_id():
    """清除路径同样拒绝分身自身实例 ID。"""
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    control = PluginLogLevelControl(
        plugin_exists=lambda plugin_id: True,
        get_instance={"DemoPluginWork": clone}.get,
        instances_for_source=lambda source_plugin_id: [],
        get_host_instance=lambda plugin_id: None,
        read_log_level=lambda _instance_id: (None, None),
        write_log_level=lambda _instance_id, _level, _expires_at: None,
    )

    with pytest.raises(LookupError):
        control.clear_level("DemoPluginWork", "DemoPluginWork")



def test_list_levels_lists_host_and_clones_for_a_real_source_plugin_id():
    """用真正的源插件 ID 查询时正常返回本体与全部分身。"""
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    control = _control(instances={"DemoPluginWork": clone})

    levels = control.list_levels("DemoPlugin")

    assert [item["instance_id"] for item in levels] == [
        "DemoPlugin",
        "DemoPluginWork",
    ]
