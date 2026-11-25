"""
验证停用插件源码存在判定与PEP610安装来源身份。
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.adapters.system.plugin.dependency import PluginDependencyInstaller
from app.runtime.extensions.plugin.catalog import PluginCatalogFacade


def test_disabled_physical_plugin_exists_without_running_instance(monkeypatch):
    """
    不带版本条件的存在性查询只需物理源码，不要求停用插件已有运行实例。
    """
    catalog = object.__new__(PluginCatalogFacade)
    catalog._plugin_instance = lambda _: None
    catalog._plugin_attr = Mock(return_value=None)
    catalog._logger = Mock()
    monkeypatch.setattr("app.runtime.extensions.plugin.catalog.importlib.util.find_spec",
                        lambda _: SimpleNamespace(origin="/isolated/demo/__init__.py"))
    assert catalog.exists("Demo")
    catalog._plugin_attr.assert_not_called()
    assert not catalog.exists("Demo", "2")
    catalog._plugin_attr.return_value = "1"
    assert not catalog.exists("Demo", "2")
    catalog._plugin_attr.return_value = "2"
    assert catalog.exists("Demo", "2")


@pytest.mark.parametrize("revision,installed_revision,commit,expected", [
    ("v1", "v1", "a" * 40, True),
    ("v1", "v2", "a" * 40, False),
    ("a" * 40, "v1", "a" * 40, True),
    ("a" * 40, "a" * 40, "b" * 40, False),
    ("feature%2Ffix", "feature/fix", "a" * 40, True),
])
def test_pep610_vcs_revision_and_commit_identity(revision, installed_revision, commit, expected):
    """
    VCS清单URL按实际仓库与revision/commit匹配，不能把错误提交当作已满足。
    """
    payload = {"url": "https://example.com/acme/pkg.git", "vcs_info": {
        "vcs": "git", "requested_revision": installed_revision, "commit_id": commit,
    }}
    distribution = SimpleNamespace(read_text=lambda _: json.dumps(payload))
    required = f"git+https://example.com/acme/pkg.git@{revision}"
    assert PluginDependencyInstaller._direct_url_matches(distribution, required) is expected


@pytest.mark.parametrize("subdirectory,expected", [("src", True), ("other", False), (None, False)])
def test_pep610_vcs_subdirectory_is_part_of_identity(subdirectory, expected):
    """
    同一仓库不同子目录的发行版不能混用。
    """
    payload = {"url": "https://example.com/pkg.git", "subdirectory": subdirectory,
               "vcs_info": {"vcs": "git", "requested_revision": "v1", "commit_id": "a" * 40}}
    distribution = SimpleNamespace(read_text=lambda _: json.dumps(payload))
    assert PluginDependencyInstaller._direct_url_matches(
        distribution, "git+https://example.com/pkg.git@v1#subdirectory=src",
    ) is expected


@pytest.mark.parametrize("hash_value,expected", [("abc123", True), ("bad", False), (None, False)])
def test_pep610_archive_hash_is_verified_in_archive_info(hash_value, expected):
    """
    归档哈希从PEP610 archive_info验证，不因去除URL片段而放宽来源约束。
    """
    payload = {"url": "https://example.com/pkg.whl",
               "archive_info": {"hashes": {"sha256": hash_value}}}
    distribution = SimpleNamespace(read_text=lambda _: json.dumps(payload))
    assert PluginDependencyInstaller._direct_url_matches(
        distribution, "https://example.com/pkg.whl#sha256=abc123",
    ) is expected
