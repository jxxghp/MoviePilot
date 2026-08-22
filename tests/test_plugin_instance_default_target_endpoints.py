"""插件实例默认调用目标的写入口（设为默认/清除默认）端点契约测试。

覆盖三层：端点对 Oper 层返回值的状态码映射（含目标缺席时原有置位不受影响）、
``response_model`` 确实透传新增的 ``is_default_target`` 字段而不被静默裁掉、
以及设置/清除后 ``resolve_plugin_instance_key`` 的解析结果随之改变的端到端链路。
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.deps import get_current_active_superuser
from app.api.endpoints.plugin import (
    clear_plugin_instance_default_target,
    list_plugin_instances,
    set_plugin_instance_default_target,
)
from app.db.models.pluginconfig import PluginConfig
from app.db.oper.pluginconfig import PluginConfigOper
from app.schemas.plugin import PluginInstanceInfo
from app.runtime.extensions.admission.instance_selection import resolve_plugin_instance_key
from app.startup.plugins_initializer import _list_plugin_instance_targets

PLUGIN_ID = "_DefaultTargetEndpointPlugin"


def _dependency_of(func, parameter_name: str):
    """读取 FastAPI 函数参数上声明的依赖函数。"""
    return inspect.signature(func).parameters[parameter_name].default.dependency


def _plugin_manager_with_instances(instance_ids):
    """构造一个 list_plugin_instances 返回给定实例集合的 PluginManager 替身。"""
    plugin_manager = MagicMock()
    plugin_manager.list_plugin_instances.return_value = [
        {"instance_id": instance_id, "instance_key": instance_id, "running": True, "state": True}
        for instance_id in instance_ids
    ]
    return plugin_manager


@pytest.fixture(autouse=True)
def _track(db):
    """把插件实例配置表纳入用例级回收。"""
    db.watermark(PluginConfig)


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #


def test_default_target_endpoints_require_superuser():
    """设为默认与清除默认两个端点必须只允许管理员访问。"""
    assert _dependency_of(set_plugin_instance_default_target, "_") is get_current_active_superuser
    assert _dependency_of(clear_plugin_instance_default_target, "_") is get_current_active_superuser


# --------------------------------------------------------------------------- #
# PUT /instances/{plugin_id}/{instance_id}/default_target
# --------------------------------------------------------------------------- #


def test_set_default_target_marks_the_target_and_clears_the_rest(db):
    """设置成功后，列表里目标实例显示为默认，其余实例不是。"""
    db.add(
        PluginConfig(plugin_id=PLUGIN_ID, instance_id="default"),
        PluginConfig(plugin_id=PLUGIN_ID, instance_id="alt"),
    )
    plugin_manager = _plugin_manager_with_instances(["default", "alt"])

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager):
        result = set_plugin_instance_default_target(PLUGIN_ID, "alt", None)
        assert result.success is True

        listing = {item["instance_id"]: item for item in list_plugin_instances(PLUGIN_ID, None)}

    assert listing["alt"]["is_default_target"] is True
    assert listing["default"]["is_default_target"] is False


def test_set_default_target_on_missing_instance_returns_404_and_keeps_previous_default(db):
    """目标实例不存在时返回 404，且不能把 Oper 层的 False 悄悄当成成功。

    Oper 层特意在目标缺席时保持原有置位不变，端点必须原样把这个失败暴露出来，
    不允许出现「旧的被清空但新的没设上」的中间态。
    """
    db.add(PluginConfig(plugin_id=PLUGIN_ID, instance_id="default", is_default_target=True))

    with pytest.raises(HTTPException) as exc_info:
        set_plugin_instance_default_target(PLUGIN_ID, "ghost", None)

    assert exc_info.value.status_code == 404
    assert PluginConfigOper().get_default_target(PLUGIN_ID).instance_id == "default"


def test_set_default_target_switches_and_clears_the_old_one(db):
    """重复设置为不同实例时，旧的置位被清掉，只剩新目标为真。"""
    db.add(
        PluginConfig(plugin_id=PLUGIN_ID, instance_id="default", is_default_target=True),
        PluginConfig(plugin_id=PLUGIN_ID, instance_id="alt"),
    )

    set_plugin_instance_default_target(PLUGIN_ID, "alt", None)

    flags = {
        row.instance_id: row.is_default_target
        for row in PluginConfigOper().list_by_plugin(PLUGIN_ID)
    }
    assert flags == {"default": False, "alt": True}


# --------------------------------------------------------------------------- #
# DELETE /instances/{plugin_id}/{instance_id}/default_target
# --------------------------------------------------------------------------- #


def test_clear_default_target_leaves_no_default(db):
    """清除后该插件不再有默认调用目标。"""
    db.add(PluginConfig(plugin_id=PLUGIN_ID, instance_id="default", is_default_target=True))

    result = clear_plugin_instance_default_target(PLUGIN_ID, "default", None)

    assert result.success is True
    assert PluginConfigOper().get_default_target(PLUGIN_ID) is None


def test_clear_default_target_is_idempotent_when_nothing_was_set(db):
    """本来就没有置位时清除仍然算成功。"""
    db.add(PluginConfig(plugin_id=PLUGIN_ID, instance_id="default"))

    result = clear_plugin_instance_default_target(PLUGIN_ID, "default", None)

    assert result.success is True
    assert PluginConfigOper().get_default_target(PLUGIN_ID) is None


def test_clear_default_target_called_twice_stays_successful(db):
    """连续清除两次都返回成功，第二次是空操作。"""
    db.add(PluginConfig(plugin_id=PLUGIN_ID, instance_id="default", is_default_target=True))

    clear_plugin_instance_default_target(PLUGIN_ID, "default", None)
    result = clear_plugin_instance_default_target(PLUGIN_ID, "default", None)

    assert result.success is True
    assert PluginConfigOper().get_default_target(PLUGIN_ID) is None


def test_clear_default_target_on_non_default_instance_does_not_touch_the_actual_default(db):
    """清除请求指定的实例若并非当前默认调用目标，视为空操作，不误清其它实例的置位。"""
    db.add(
        PluginConfig(plugin_id=PLUGIN_ID, instance_id="default", is_default_target=True),
        PluginConfig(plugin_id=PLUGIN_ID, instance_id="alt"),
    )

    result = clear_plugin_instance_default_target(PLUGIN_ID, "alt", None)

    assert result.success is True
    assert PluginConfigOper().get_default_target(PLUGIN_ID).instance_id == "default"


# --------------------------------------------------------------------------- #
# response_model 不得静默裁掉新增字段
# --------------------------------------------------------------------------- #


def test_response_model_keeps_the_is_default_target_field(db):
    """``PluginInstanceInfo`` 补的 ``is_default_target`` 字段必须原样穿过 response_model。

    FastAPI 的 response_model 校验/序列化等价于对返回字典执行一次
    ``Model(**payload).model_dump()``；字段若没声明在模型里，会在这一步被静默裁掉。
    """
    db.add(
        PluginConfig(plugin_id=PLUGIN_ID, instance_id="default"),
        PluginConfig(plugin_id=PLUGIN_ID, instance_id="alt"),
    )
    set_plugin_instance_default_target(PLUGIN_ID, "alt", None)
    plugin_manager = _plugin_manager_with_instances(["default", "alt"])

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager):
        payload = list_plugin_instances(PLUGIN_ID, None)

    serialized = {
        item["instance_id"]: PluginInstanceInfo(**item).model_dump() for item in payload
    }
    for item in payload:
        assert set(serialized[item["instance_id"]]) == set(item)
    assert serialized["alt"]["is_default_target"] is True
    assert serialized["default"]["is_default_target"] is False


# --------------------------------------------------------------------------- #
# 端到端：写入口与调用目标解析的联动
# --------------------------------------------------------------------------- #


def test_setting_default_target_lets_resolve_plugin_instance_key_succeed(db, monkeypatch):
    """设为默认前调用目标解析报错，设置后立即能解析到该实例键——这正是本字段存在的意义。"""
    import app.runtime.extensions.admission.instance_selection as instance_selection_module

    db.add(
        PluginConfig(plugin_id=PLUGIN_ID, instance_id="default", is_enabled=True),
        PluginConfig(plugin_id=PLUGIN_ID, instance_id="alt", is_enabled=True),
    )
    monkeypatch.setattr(
        instance_selection_module, "_instance_target_lister", _list_plugin_instance_targets
    )

    with pytest.raises(LookupError):
        resolve_plugin_instance_key(PLUGIN_ID)

    set_plugin_instance_default_target(PLUGIN_ID, "alt", None)

    assert resolve_plugin_instance_key(PLUGIN_ID) == f"{PLUGIN_ID}@alt"


def test_clearing_default_target_makes_resolve_fail_again(db, monkeypatch):
    """清除默认之后，原本依赖默认解析的调用立即回到报错状态，没有过渡期。"""
    import app.runtime.extensions.admission.instance_selection as instance_selection_module

    db.add(PluginConfig(plugin_id=PLUGIN_ID, instance_id="default", is_enabled=True))
    monkeypatch.setattr(
        instance_selection_module, "_instance_target_lister", _list_plugin_instance_targets
    )
    set_plugin_instance_default_target(PLUGIN_ID, "default", None)
    assert resolve_plugin_instance_key(PLUGIN_ID) == PLUGIN_ID

    clear_plugin_instance_default_target(PLUGIN_ID, "default", None)

    with pytest.raises(LookupError):
        resolve_plugin_instance_key(PLUGIN_ID)
