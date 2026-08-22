"""扩展安装挂载点 ``app/plugins/`` 的纯数据目录契约。

容器里 ``docker run -v /宿主目录:/app/app/plugins`` 会用宿主目录整体覆盖挂载点，
被覆盖的一切当场消失。因此挂载点里不能有任何宿主源码：连 ``__init__.py`` 都不放，
``app.plugins`` 是命名空间包。存量扩展写的 ``from app.plugins import _PluginBase``
由 ``app/runtime/compat`` 的符号别名解析，与挂载点内容无关。
"""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest

from app.sdk.extension import _PluginBase

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "app" / "plugins"
# 参考实现里依赖最少的一个，用于验证挂载覆盖后仍能按 app.plugins.<id> 导入
REFERENCE_PLUGIN = "servicehealth"
# 存量扩展的既有写法，必须继续可用
LEGACY_PLUGIN_SOURCE = """
from app.plugins import _PluginBase


class MountedPlugin(_PluginBase):
    plugin_name = "MountedPlugin"

    def init_plugin(self, config=None):
        pass

    def get_state(self) -> bool:
        return True
"""


@pytest.fixture
def mounted_plugin_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 ``app.plugins`` 的搜索路径整体换成临时目录，复现卷挂载覆盖挂载点的效果。

    :param tmp_path: 用例级临时目录
    :param monkeypatch: 用例级补丁器
    :return: 冒充宿主挂载目录的临时目录
    """
    plugins_package = importlib.import_module("app.plugins")
    mount = tmp_path / "mounted-plugins"
    mount.mkdir()
    # 挂载期间导入的是临时目录里的副本，与本进程已加载的同名扩展不是同一个类对象；
    # 原有模块与宿主包上的子模块属性整份存下、退出时原样放回，避免污染其他用例
    preserved = {
        name: module
        for name, module in sys.modules.items()
        if name.startswith("app.plugins.")
    }
    for name in preserved:
        del sys.modules[name]
    monkeypatch.setattr(plugins_package, "__path__", [str(mount)])
    importlib.invalidate_caches()
    yield mount
    for name in [name for name in sys.modules if name.startswith("app.plugins.")]:
        del sys.modules[name]
    sys.modules.update(preserved)
    for name, module in preserved.items():
        child = name.split(".")[2]
        if name.count(".") == 2:
            setattr(plugins_package, child, module)
    importlib.invalidate_caches()


def test_mount_point_holds_no_host_source() -> None:
    """挂载点顶层没有任何 Python 源码文件，只有扩展各自的目录。"""
    assert not (PLUGIN_ROOT / "__init__.py").exists()

    stray = sorted(
        entry.name for entry in PLUGIN_ROOT.iterdir() if entry.suffix == ".py"
    )

    assert stray == [], (
        f"挂载点顶层出现宿主源码 {stray}；这些文件会被卷挂载整个盖掉，"
        "应改放到 app/sdk 或 app/runtime，并在 app/runtime/compat 登记旧路径。"
    )


def test_plugins_package_resolves_without_an_init_file() -> None:
    """``app.plugins`` 以命名空间包身份解析，没有模块文件。"""
    plugins_package = importlib.import_module("app.plugins")

    assert getattr(plugins_package, "__file__", None) is None
    assert list(plugins_package.__path__)


def test_legacy_plugin_base_import_still_resolves() -> None:
    """v2 生态的 ``from app.plugins import _PluginBase`` 仍解析到同一个类。"""
    from app.plugins import _PluginBase as legacy_base

    assert legacy_base is _PluginBase


def test_legacy_plugin_chain_and_instance_path_still_resolve() -> None:
    """挂载点上其余旧符号同样由兼容层解析，不随 __init__.py 一起消失。"""
    from app.plugins import PluginChian, plugin_instance_path
    from app.runtime.extensions.lifecycle.paths import (
        plugin_instance_path as canonical_path_helper,
    )
    from app.sdk.extension import PluginChian as canonical_chain

    assert PluginChian is canonical_chain
    assert plugin_instance_path is canonical_path_helper


def test_unregistered_names_are_not_invented() -> None:
    """兼容层只解析已登记的旧符号，其余名字照常报错。"""
    with pytest.raises(ImportError):
        from app.plugins import NotAPluginSymbol  # noqa: F401


def test_reference_plugin_loads_from_a_mounted_directory(
    mounted_plugin_root: Path,
) -> None:
    """挂载目录整体覆盖挂载点后，参考实现仍按 ``app.plugins.<id>`` 导入。"""
    shutil.copytree(
        PLUGIN_ROOT / REFERENCE_PLUGIN,
        mounted_plugin_root / REFERENCE_PLUGIN,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    importlib.invalidate_caches()

    module = importlib.import_module(f"app.plugins.{REFERENCE_PLUGIN}")

    assert Path(module.__file__).is_relative_to(mounted_plugin_root)


def test_legacy_plugin_source_loads_from_a_mounted_directory(
    mounted_plugin_root: Path,
) -> None:
    """挂载目录里的扩展用 v2 写法继承基类也能加载。"""
    plugin_dir = mounted_plugin_root / "mountedplugin"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(LEGACY_PLUGIN_SOURCE, encoding="utf-8")
    importlib.invalidate_caches()

    module = importlib.import_module("app.plugins.mountedplugin")

    assert issubclass(module.MountedPlugin, _PluginBase)
    assert Path(module.__file__).is_relative_to(mounted_plugin_root)
