"""
插件实例与安装来源的 ID 合同一致性回归测试。
"""

import pytest
from pydantic import ValidationError

from app.domain.plugin import is_physical_plugin_id
from app.schemas.plugin import PluginInstance, validate_plugin_id


@pytest.mark.parametrize("plugin_id", ["DemoPlugin", "nullbr_search", "Nullbr_Search2", "A" * 128])
def test_instance_ids_accept_installed_plugin_identity(plugin_id: str) -> None:
    """
    市场允许的物理身份必须能用于本体及共享源码分身，且不改变大小写。
    """
    assert is_physical_plugin_id(plugin_id)
    assert validate_plugin_id(plugin_id) == plugin_id
    host = PluginInstance(instance_id=plugin_id, source_plugin_id=plugin_id)
    assert host.is_host
    assert host.instance_id == plugin_id
    if len(plugin_id) < 128:
        clone = PluginInstance(instance_id=f"{plugin_id}x2", source_plugin_id=plugin_id)
        assert not clone.is_host
        assert clone.source_plugin_id == plugin_id


@pytest.mark.parametrize(
    "plugin_id",
    ["", "_Plugin", "1Plugin", "../Plugin", "a/b", "a\\b", "a.b", "a-b", "a@b",
     "a b", "a\n", "插件", "Ｐlugin", "A" * 129],
)
def test_instance_ids_reject_unsafe_or_unsupported_identity(plugin_id: str) -> None:
    """
    实例校验保持字母开头、ASCII、路径安全和长度限制，与安装侧一致。
    """
    assert not is_physical_plugin_id(plugin_id)
    with pytest.raises(ValueError):
        validate_plugin_id(plugin_id)
    with pytest.raises(ValidationError):
        PluginInstance(instance_id=plugin_id, source_plugin_id="DemoPlugin")
    with pytest.raises(ValidationError):
        PluginInstance(instance_id="DemoPlugin", source_plugin_id=plugin_id)
