"""插件数据目录的路径安全测试。"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.runtime.extensions.lifecycle import paths as plugin_paths_module
from app.foundation.paths import ensure_path_segment, is_safe_path_segment
from app.sdk.extension import _PluginBase


class _SamplePlugin(_PluginBase):
    """只实现抽象合同的最小插件。"""

    def init_plugin(self, config: dict = None):
        """生效配置信息。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return True

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 声明。"""
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单。"""
        return None, {}

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页。"""
        return None

    def stop_service(self):
        """停止插件服务。"""


@pytest.fixture
def plugin_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """把插件数据根目录指向临时目录。"""
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setattr(
        plugin_paths_module,
        "settings",
        SimpleNamespace(PLUGIN_DATA_PATH=root),
    )
    return root


@pytest.mark.parametrize(
    "plugin_id",
    [
        "SamplePlugin",
        "Sample_Plugin",
        "Sample-Plugin",
        "Sample.Plugin",
        # 插件ID只做目录名安全校验，"@" 不做实例含义解析；实例隔离体现在插件ID
        # 之下的实例层（本用例落到 <plugin_id>/default/data）。
        "SamplePlugin@livingroom",
        "插件分身",
        "a" * 200,
    ],
)
def test_normal_plugin_id_is_accepted(
    plugin_data_root: Path,
    plugin_id: str,
) -> None:
    """存量插件的正常标识仍能建立默认实例的数据目录。"""
    data_path = _SamplePlugin().get_data_path(plugin_id)

    assert data_path == plugin_data_root / plugin_id / "default" / "data"
    assert data_path.is_dir()
    assert data_path.parent.parent == plugin_data_root / plugin_id


def test_default_plugin_id_uses_class_name(plugin_data_root: Path) -> None:
    """未传插件标识时按插件类名建立默认实例的数据目录。"""
    data_path = _SamplePlugin().get_data_path()

    assert data_path == plugin_data_root / "_SamplePlugin" / "default" / "data"
    assert data_path.is_dir()


@pytest.mark.parametrize(
    "plugin_id",
    [
        "..",
        ".",
        "../other",
        "../../etc/passwd",
        "nested/child",
        "nested\\child",
        "/absolute",
        "\\absolute",
        "C:\\Windows",
        "C:",
        "\x00",
        "bad\x00id",
    ],
)
def test_dangerous_plugin_id_is_rejected(
    plugin_data_root: Path,
    plugin_id: str,
) -> None:
    """能逃出数据根目录的插件标识被拒绝，且不留下任何目录。"""
    with pytest.raises(ValueError):
        _SamplePlugin().get_data_path(plugin_id)

    assert list(plugin_data_root.iterdir()) == []


@pytest.mark.parametrize(
    "value",
    ["SamplePlugin", "插件分身", "Sample Plugin", "Sample@1", "Sample.Plugin"],
)
def test_safe_path_segment_accepts_single_directory_name(value: str) -> None:
    """单层普通目录名被判定为安全。"""
    assert is_safe_path_segment(value) is True
    assert ensure_path_segment(value, subject="插件ID") == value


@pytest.mark.parametrize(
    "value",
    ["", ".", "..", "a/b", "a\\b", "/a", "C:\\a", "C:", "\x00", None, 1],
)
def test_safe_path_segment_rejects_traversal_and_roots(value: object) -> None:
    """空值、分隔符、盘符与上级目录引用被判定为不安全。"""
    assert is_safe_path_segment(value) is False
    with pytest.raises(ValueError):
        ensure_path_segment(value, subject="插件ID")
