"""插件联邦远程入口按实例绑定版本区分地址与标识的行为测试。

Module Federation 的远程名是浏览器端全局单一键空间，同一插件的两个版本若共用
同一地址或标识会在浏览器端互相覆盖。覆盖范围：``get_plugin_remote_entry`` 按
传入版本号解析到对应版本目录、指定版本目录缺失时回落到插件当前安装版本、以及
``PluginProjection`` 通过真实的入口生成器把实例绑定的版本接到地址与标识两端。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.extensions import plugin_manager as plugin_manager_module
from app.runtime.extensions.lifecycle.layout import (
    plugin_version_dir_name,
    register_plugin_version,
)
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager

PLUGIN_ID = "RemoteVersionedPlugin"
PLUGIN_DIR = PLUGIN_ID.lower()
SECOND_KEY = f"{PLUGIN_ID}@second"


@pytest.fixture
def plugins_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把插件根目录指向临时目录，供联邦入口地址解析读取版本目录。

    :param tmp_path: 用例级临时目录
    :param monkeypatch: 用例级补丁器
    :return: 临时插件根目录
    """
    monkeypatch.setattr(
        plugin_manager_module, "settings", SimpleNamespace(ROOT_PATH=tmp_path)
    )
    root = tmp_path / "app" / "plugins"
    root.mkdir(parents=True)
    return root


def _make_version_dir(plugins_root: Path, version: str, *, register: bool = False) -> Path:
    """在插件根目录下建出一个版本目录，使地址解析能发现该版本已落地。

    :param plugins_root: 插件根目录
    :param version: 版本号
    :param register: 是否把该版本登记为版本元信息中的当前版本
    :return: 版本目录
    """
    plugin_root = plugins_root / PLUGIN_DIR
    dir_name = plugin_version_dir_name(version)
    version_dir = plugin_root / dir_name
    version_dir.mkdir(parents=True, exist_ok=True)
    if register:
        register_plugin_version(plugin_root, version, dir_name, source="test")
    return version_dir


class _VuePlugin(SimpleNamespace):
    """声明 Vue 联邦渲染模式的运行态插件替身。"""

    def get_render_mode(self):
        """声明使用 Vue 联邦组件渲染，构建产物固定在 dist/assets 下。"""
        return "vue", "dist/assets"


def test_get_plugin_remote_entry_resolves_the_given_version(plugins_root):
    """显式传入版本号时，地址解析到该版本自己的目录，而不是插件当前安装版本。"""
    _make_version_dir(plugins_root, "1.0.0", register=True)
    _make_version_dir(plugins_root, "2.0.0")

    pinned_url = PluginManager.get_plugin_remote_entry(
        PLUGIN_ID, "dist/assets", version="2.0.0"
    )
    current_url = PluginManager.get_plugin_remote_entry(PLUGIN_ID, "dist/assets")

    assert pinned_url == f"/plugin/file/{PLUGIN_DIR}/v2_0_0/dist/assets/remoteEntry.js"
    assert current_url == f"/plugin/file/{PLUGIN_DIR}/v1_0_0/dist/assets/remoteEntry.js"
    assert pinned_url != current_url


def test_get_plugin_remote_entry_falls_back_when_the_pinned_version_dir_is_missing(
    plugins_root,
):
    """绑定的版本目录已被回收或从未落地时，回落到插件当前安装版本，不报错。"""
    _make_version_dir(plugins_root, "1.0.0", register=True)

    url = PluginManager.get_plugin_remote_entry(PLUGIN_ID, "dist/assets", version="9.9.9")

    assert url == f"/plugin/file/{PLUGIN_DIR}/v1_0_0/dist/assets/remoteEntry.js"


def test_sibling_instances_bound_to_different_versions_get_distinct_remote_entries(
    plugins_root,
):
    """两个实例各自绑不同版本时，经真实的入口生成器得到不同地址与不同标识。

    直接把 ``PluginManager.get_plugin_remote_entry`` 接成 ``PluginProjection`` 的
    入口生成器，而不是测试替身里手写的假实现，因此能捕获两者之间参数个数或顺序
    不匹配这类接线错误；版本号从插件实例的 ``plugin_version`` 属性读出。
    """
    _make_version_dir(plugins_root, "1.0.0", register=True)
    _make_version_dir(plugins_root, "2.0.0")

    default_instance = _VuePlugin(plugin_name="多版本插件", plugin_version="1.0.0")
    second_instance = _VuePlugin(plugin_name="多版本插件", plugin_version="2.0.0")
    projection = PluginProjection(
        {PLUGIN_ID: default_instance, SECOND_KEY: second_instance},
        remote_entry_factory=PluginManager.get_plugin_remote_entry,
    )

    remotes = {item["id"]: item for item in projection.remotes()}

    assert remotes[PLUGIN_ID]["url"].endswith("v1_0_0/dist/assets/remoteEntry.js")
    assert remotes[SECOND_KEY]["url"].endswith("v2_0_0/dist/assets/remoteEntry.js")
    assert remotes[PLUGIN_ID]["url"] != remotes[SECOND_KEY]["url"]
    assert remotes[PLUGIN_ID]["remote_key"] == f"{PLUGIN_ID}#1.0.0"
    assert remotes[SECOND_KEY]["remote_key"] == f"{SECOND_KEY}#2.0.0"
    assert remotes[PLUGIN_ID]["remote_key"] != remotes[SECOND_KEY]["remote_key"]
    # id、url、name 三个既有字段保持原语义，未声明版本区分标识的旧前端仍可读取
    assert remotes[PLUGIN_ID]["id"] == PLUGIN_ID
    assert remotes[SECOND_KEY]["id"] == SECOND_KEY
