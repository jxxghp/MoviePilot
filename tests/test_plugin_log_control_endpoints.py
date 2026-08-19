"""插件专属日志控制端点（查询/设置/清除等级、列出日志文件）的行为契约测试。"""

import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.endpoints.plugin import (
    clear_plugin_instance_log_level_api,
    plugin_instance_log_files,
    plugin_instance_log_levels,
    set_plugin_instance_log_level_api,
)
from app.schemas.plugin import PluginInstanceLogLevelSet


def _plugin_manager_with_instances(instance_ids):
    """构造一个 list_plugin_instances 返回给定实例集合的 PluginManager 替身。"""
    plugin_manager = MagicMock()
    plugin_manager.list_plugin_instances.return_value = [
        {"instance_id": instance_id, "instance_key": instance_id, "running": True, "state": True}
        for instance_id in instance_ids
    ]
    return plugin_manager


# ---------------------------------------------------------------------------
# GET /loglevel/{plugin_id}
# ---------------------------------------------------------------------------


def test_plugin_instance_log_levels_returns_configured_and_effective():
    """列表端点应逐实例返回配置的覆盖值和当前生效等级。"""
    plugin_manager = _plugin_manager_with_instances(["default", "second"])
    expires_at = datetime(2030, 1, 1)

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager), \
            patch(
                "app.api.endpoints.plugin.get_plugin_instance_log_level_override",
                side_effect=lambda pid, iid: ("DEBUG", expires_at) if iid == "second" else None,
            ), \
            patch(
                "app.api.endpoints.plugin.get_effective_plugin_instance_log_level",
                side_effect=lambda pid, iid: "DEBUG" if iid == "second" else "INFO",
            ):
        result = plugin_instance_log_levels("DemoPlugin", None)

    by_instance = {item.instance_id: item for item in result}
    assert by_instance["default"].configured_level is None
    assert by_instance["default"].effective_level == "INFO"
    assert by_instance["second"].configured_level == "DEBUG"
    assert by_instance["second"].expires_at == expires_at
    assert by_instance["second"].effective_level == "DEBUG"


def test_plugin_instance_log_levels_maps_unknown_plugin_to_404():
    """插件不存在时查询端点返回 404。"""
    plugin_manager = MagicMock()
    plugin_manager.list_plugin_instances.side_effect = LookupError("插件 DemoPlugin 不存在")

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager):
        with pytest.raises(HTTPException) as exc_info:
            plugin_instance_log_levels("DemoPlugin", None)

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# PUT /loglevel/{plugin_id}/{instance_id}
# ---------------------------------------------------------------------------


def test_set_plugin_instance_log_level_api_persists_and_activates():
    """设置成功时应同时写入配置表并让日志模块立即生效。"""
    plugin_manager = _plugin_manager_with_instances(["second"])
    config_oper = MagicMock()

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager), \
            patch("app.api.endpoints.plugin.PluginConfigOper", return_value=config_oper), \
            patch("app.api.endpoints.plugin.set_plugin_instance_log_level") as set_level:
        result = set_plugin_instance_log_level_api(
            "DemoPlugin", "second", PluginInstanceLogLevelSet(level="debug"), None
        )

    assert result.success is True
    config_oper.upsert.assert_called_once_with(
        "DemoPlugin", "second", {"log_level": "DEBUG", "log_expires_at": None}
    )
    set_level.assert_called_once_with("DemoPlugin", "second", "DEBUG", None)


def test_set_plugin_instance_log_level_api_rejects_invalid_level_with_400():
    """非法等级取值应返回 400，且不触达数据库或日志模块。"""
    plugin_manager = _plugin_manager_with_instances(["second"])
    config_oper = MagicMock()

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager), \
            patch("app.api.endpoints.plugin.PluginConfigOper", return_value=config_oper), \
            patch("app.api.endpoints.plugin.set_plugin_instance_log_level") as set_level:
        with pytest.raises(HTTPException) as exc_info:
            set_plugin_instance_log_level_api(
                "DemoPlugin", "second", PluginInstanceLogLevelSet(level="TRACE"), None
            )

    assert exc_info.value.status_code == 400
    config_oper.upsert.assert_not_called()
    set_level.assert_not_called()


def test_set_plugin_instance_log_level_api_maps_unknown_plugin_to_404():
    """插件不存在时设置端点返回 404。"""
    plugin_manager = MagicMock()
    plugin_manager.list_plugin_instances.side_effect = LookupError("插件 DemoPlugin 不存在")

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager):
        with pytest.raises(HTTPException) as exc_info:
            set_plugin_instance_log_level_api(
                "DemoPlugin", "second", PluginInstanceLogLevelSet(level="DEBUG"), None
            )

    assert exc_info.value.status_code == 404


def test_set_plugin_instance_log_level_api_maps_unknown_instance_to_404():
    """实例标识未登记时设置端点返回 404。"""
    plugin_manager = _plugin_manager_with_instances(["default"])

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager):
        with pytest.raises(HTTPException) as exc_info:
            set_plugin_instance_log_level_api(
                "DemoPlugin", "ghost", PluginInstanceLogLevelSet(level="DEBUG"), None
            )

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /loglevel/{plugin_id}/{instance_id}
# ---------------------------------------------------------------------------


def test_clear_plugin_instance_log_level_api_persists_and_deactivates():
    """清除成功时应同步清空配置表并让日志模块立即回落全局等级。"""
    plugin_manager = _plugin_manager_with_instances(["second"])
    config_oper = MagicMock()

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager), \
            patch("app.api.endpoints.plugin.PluginConfigOper", return_value=config_oper), \
            patch("app.api.endpoints.plugin.clear_plugin_instance_log_level") as clear_level:
        result = clear_plugin_instance_log_level_api("DemoPlugin", "second", None)

    assert result.success is True
    config_oper.upsert.assert_called_once_with(
        "DemoPlugin", "second", {"log_level": None, "log_expires_at": None}
    )
    clear_level.assert_called_once_with("DemoPlugin", "second")


def test_clear_plugin_instance_log_level_api_maps_unknown_instance_to_404():
    """实例标识未登记时清除端点返回 404。"""
    plugin_manager = _plugin_manager_with_instances(["default"])

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager):
        with pytest.raises(HTTPException) as exc_info:
            clear_plugin_instance_log_level_api("DemoPlugin", "ghost", None)

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# GET /logfiles/{plugin_id}/{instance_id}
# ---------------------------------------------------------------------------


def test_plugin_instance_log_files_lists_directory_sorted_by_mtime(tmp_path):
    """日志文件列表应按修改时间倒序返回，并附带大小与时间信息。"""
    plugin_manager = _plugin_manager_with_instances(["second"])
    older = tmp_path / "plugin.log.1"
    newer = tmp_path / "plugin.log"
    older.write_text("old", encoding="utf-8")
    time.sleep(0.01)
    newer.write_text("current content", encoding="utf-8")

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager), \
            patch("app.api.endpoints.plugin.get_plugin_instance_log_dir", return_value=tmp_path):
        result = plugin_instance_log_files("DemoPlugin", "second", None)

    assert [item.name for item in result] == ["plugin.log", "plugin.log.1"]
    assert result[0].size == len("current content".encode("utf-8"))


def test_plugin_instance_log_files_returns_empty_list_when_dir_missing(tmp_path):
    """实例合法但尚未写过日志（目录不存在）时返回空列表而不是报错。"""
    plugin_manager = _plugin_manager_with_instances(["second"])
    missing_dir = tmp_path / "does-not-exist"

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager), \
            patch("app.api.endpoints.plugin.get_plugin_instance_log_dir", return_value=missing_dir):
        result = plugin_instance_log_files("DemoPlugin", "second", None)

    assert result == []


def test_plugin_instance_log_files_maps_unknown_plugin_to_404():
    """插件不存在时日志文件列表端点返回 404。"""
    plugin_manager = MagicMock()
    plugin_manager.list_plugin_instances.side_effect = LookupError("插件 DemoPlugin 不存在")

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager):
        with pytest.raises(HTTPException) as exc_info:
            plugin_instance_log_files("DemoPlugin", "second", None)

    assert exc_info.value.status_code == 404


def test_plugin_instance_log_files_maps_unknown_instance_to_404():
    """实例标识未登记时日志文件列表端点返回 404。"""
    plugin_manager = _plugin_manager_with_instances(["default"])

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager):
        with pytest.raises(HTTPException) as exc_info:
            plugin_instance_log_files("DemoPlugin", "ghost", None)

    assert exc_info.value.status_code == 404
