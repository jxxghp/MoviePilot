"""插件实例增删查端点的行为契约测试。

覆盖列表/创建/删除三个端点对管理器异常的状态码映射，以及创建与删除成功后
必须调用 register_plugin 整体重建该插件的定时服务、命令与接口。
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.endpoints.plugin import (
    create_plugin_instance,
    delete_plugin_instance,
    list_plugin_instances,
)
from app.schemas.plugin import PluginInstanceCreate


def test_list_plugin_instances_returns_manager_result():
    """列表端点直接透传管理器返回的实例信息。"""
    plugin_manager = MagicMock()
    plugin_manager.list_plugin_instances.return_value = [
        {"instance_id": "default", "instance_key": "DemoPlugin", "running": True, "state": True}
    ]

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager):
        result = list_plugin_instances("DemoPlugin", None)

    assert result == plugin_manager.list_plugin_instances.return_value
    plugin_manager.list_plugin_instances.assert_called_once_with("DemoPlugin")


def test_list_plugin_instances_maps_unknown_plugin_to_404():
    """插件不存在时列表端点返回 404。"""
    plugin_manager = MagicMock()
    plugin_manager.list_plugin_instances.side_effect = LookupError("插件 DemoPlugin 不存在")

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager):
        with pytest.raises(HTTPException) as exc_info:
            list_plugin_instances("DemoPlugin", None)

    assert exc_info.value.status_code == 404


def test_create_plugin_instance_rebuilds_registrations_on_success():
    """创建成功后端点必须调用 register_plugin 整体重建定时服务、命令与接口。"""
    plugin_manager = MagicMock()
    info = {"instance_id": "second", "instance_key": "DemoPlugin@second", "running": True, "state": True}
    plugin_manager.create_plugin_instance.return_value = info
    registered = []

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager), \
            patch("app.api.endpoints.plugin.register_plugin", side_effect=registered.append):
        result = create_plugin_instance(
            "DemoPlugin", PluginInstanceCreate(instance_id="second", config={"enable": True}), None
        )

    assert result == info
    plugin_manager.create_plugin_instance.assert_called_once_with("DemoPlugin", "second", {"enable": True})
    assert registered == ["DemoPlugin"]


def test_create_plugin_instance_maps_unknown_plugin_to_404_without_rebuild():
    """插件不存在时创建端点返回 404，且不触发注册重建。"""
    plugin_manager = MagicMock()
    plugin_manager.create_plugin_instance.side_effect = LookupError("插件 DemoPlugin 不存在")
    registered = []

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager), \
            patch("app.api.endpoints.plugin.register_plugin", side_effect=registered.append):
        with pytest.raises(HTTPException) as exc_info:
            create_plugin_instance("DemoPlugin", PluginInstanceCreate(instance_id="second"), None)

    assert exc_info.value.status_code == 404
    assert registered == []


def test_create_plugin_instance_maps_invalid_instance_id_to_400_without_rebuild():
    """实例标识非法（如路径穿越）时创建端点返回 400，且不触发注册重建。"""
    plugin_manager = MagicMock()
    plugin_manager.create_plugin_instance.side_effect = ValueError("非法的插件实例ID：'../../etc'")
    registered = []

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager), \
            patch("app.api.endpoints.plugin.register_plugin", side_effect=registered.append):
        with pytest.raises(HTTPException) as exc_info:
            create_plugin_instance("DemoPlugin", PluginInstanceCreate(instance_id="../../etc"), None)

    assert exc_info.value.status_code == 400
    assert registered == []


def test_delete_plugin_instance_rebuilds_registrations_on_success():
    """删除成功后端点必须调用 register_plugin 整体重建定时服务、命令与接口。"""
    plugin_manager = MagicMock()
    registered = []

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager), \
            patch("app.api.endpoints.plugin.register_plugin", side_effect=registered.append):
        result = delete_plugin_instance("DemoPlugin", "second", None)

    assert result.success is True
    plugin_manager.delete_plugin_instance.assert_called_once_with("DemoPlugin", "second")
    assert registered == ["DemoPlugin"]


def test_delete_plugin_instance_maps_default_instance_rejection_to_400():
    """删除默认实例时端点返回 400，且不触发注册重建。"""
    plugin_manager = MagicMock()
    plugin_manager.delete_plugin_instance.side_effect = ValueError("默认实例不可删除")
    registered = []

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager), \
            patch("app.api.endpoints.plugin.register_plugin", side_effect=registered.append):
        with pytest.raises(HTTPException) as exc_info:
            delete_plugin_instance("DemoPlugin", "default", None)

    assert exc_info.value.status_code == 400
    assert registered == []


def test_delete_plugin_instance_maps_unknown_instance_to_404():
    """删除不存在的实例时端点返回 404。"""
    plugin_manager = MagicMock()
    plugin_manager.delete_plugin_instance.side_effect = LookupError("插件实例不存在")
    registered = []

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager), \
            patch("app.api.endpoints.plugin.register_plugin", side_effect=registered.append):
        with pytest.raises(HTTPException) as exc_info:
            delete_plugin_instance("DemoPlugin", "ghost", None)

    assert exc_info.value.status_code == 404
    assert registered == []
