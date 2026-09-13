"""插件实例日志等级控制面的身份校验与读写分发测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.runtime import log as log_module
from app.runtime.extensions.plugin.loglevel import PluginLogLevelControl
from app.runtime.log import get_plugin_instance_log_level_override
from app.schemas.plugin import PluginInstance


@pytest.fixture(autouse=True)
def _isolate_plugin_log_state(monkeypatch):
    """隔离进程内覆盖缓存与全局等级，避免与其他用例的实例 ID 相互污染。"""
    monkeypatch.setattr(log_module, "_plugin_level_overrides", {})
    monkeypatch.setattr(log_module.log_settings, "DEBUG", False)
    monkeypatch.setattr(log_module.log_settings, "LOG_LEVEL", "INFO")
    yield


def _control(
    *,
    instances: dict[str, PluginInstance],
    stored: dict | None = None,
    writes: list | None = None,
) -> PluginLogLevelControl:
    """按测试所需装配最小依赖，配置侧读写落在内存字典上。"""
    stored = stored if stored is not None else {}
    writes = writes if writes is not None else []

    def _write(instance_id: str, level, expires_at) -> None:
        """记录一次落盘写入并同步内存中的已存值。"""
        writes.append((instance_id, level, expires_at))
        if level is None and expires_at is None:
            stored.pop(instance_id, None)
        else:
            stored[instance_id] = (level, expires_at)

    return PluginLogLevelControl(
        plugin_exists=lambda plugin_id: True,
        get_instance=instances.get,
        instances_for_source=lambda source_plugin_id: [
            instance
            for instance in instances.values()
            if instance.source_plugin_id == source_plugin_id
        ],
        read_log_level=lambda instance_id: stored.get(instance_id, (None, None)),
        write_log_level=_write,
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
    control = _control(instances={"DemoPluginWork": clone})

    with pytest.raises(LookupError):
        control.set_level("DemoPluginWork", "DemoPluginWork", "DEBUG")


def test_clear_level_rejects_clone_own_id_as_plugin_id():
    """清除路径同样拒绝分身自身实例 ID。"""
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    control = _control(instances={"DemoPluginWork": clone})

    with pytest.raises(LookupError):
        control.clear_level("DemoPluginWork", "DemoPluginWork")


def test_set_level_rejects_instance_of_another_plugin():
    """实例必须真实归属被点名的源插件，跨插件写入要拒绝。"""
    foreign = PluginInstance(instance_id="OtherWork", source_plugin_id="OtherPlugin")
    control = _control(instances={"OtherWork": foreign})

    with pytest.raises(LookupError):
        control.set_level("DemoPlugin", "OtherWork", "DEBUG")


def test_list_levels_lists_host_and_clones_for_a_real_source_plugin_id():
    """用真正的源插件 ID 查询时正常返回本体与全部分身。"""
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    control = _control(instances={"DemoPluginWork": clone})

    levels = control.list_levels("DemoPlugin")

    assert [item["instance_id"] for item in levels] == [
        "DemoPlugin",
        "DemoPluginWork",
    ]
    assert all(item["effective_level"] == "INFO" for item in levels)
    assert all(item["configured_level"] is None for item in levels)


def test_set_level_writes_cache_and_storage_then_clear_reverts_both():
    """设置要同时落进程内缓存与配置行，清除后两处都回到未设置。"""
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    stored: dict = {}
    writes: list = []
    control = _control(instances={"DemoPluginWork": clone}, stored=stored, writes=writes)

    control.set_level("DemoPlugin", "DemoPluginWork", "debug")

    assert get_plugin_instance_log_level_override("DemoPluginWork") == ("DEBUG", None)
    assert writes == [("DemoPluginWork", "DEBUG", None)]

    control.clear_level("DemoPlugin", "DemoPluginWork")

    assert get_plugin_instance_log_level_override("DemoPluginWork") is None
    assert writes[-1] == ("DemoPluginWork", None, None)
    assert stored == {}


def test_clear_level_skips_storage_write_when_nothing_was_configured():
    """从未设置过覆盖时清除不该产生一次多余的落盘写入。"""
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    writes: list = []
    control = _control(instances={"DemoPluginWork": clone}, writes=writes)

    control.clear_level("DemoPlugin", "DemoPluginWork")

    assert writes == []


def test_set_level_normalizes_naive_expiry_to_utc_before_persisting():
    """裸时间在写入侧就归一到 UTC，落盘值与缓存读回值描述同一个时刻。"""
    clone = PluginInstance(instance_id="DemoPluginWork", source_plugin_id="DemoPlugin")
    writes: list = []
    control = _control(instances={"DemoPluginWork": clone}, writes=writes)
    naive = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)

    control.set_level("DemoPlugin", "DemoPluginWork", "WARNING", naive)

    _instance_id, _level, persisted_expiry = writes[0]
    assert persisted_expiry == naive.replace(tzinfo=timezone.utc)
    override = get_plugin_instance_log_level_override("DemoPluginWork")
    assert override is not None
    assert override[1] == persisted_expiry


def test_set_level_on_host_uses_plugin_id_as_instance_id():
    """本体自身不需要实例行也能设置覆盖，实例 ID 就是插件 ID。"""
    writes: list = []
    control = _control(instances={}, writes=writes)

    control.set_level("DemoPlugin", "DemoPlugin", "ERROR")

    assert writes == [("DemoPlugin", "ERROR", None)]
    assert get_plugin_instance_log_level_override("DemoPlugin") == ("ERROR", None)
