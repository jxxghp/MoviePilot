"""索引、字幕、过滤扩展经端口取用宿主服务的行为验证。

覆盖端口未注册时的报错、组合根注册后端口解析到可用实现，
以及扩展模块经端口取用宿主服务的集成路径。
"""

from unittest.mock import patch

import pytest

from app.modules.filter import FilterModule
from app.modules.indexer import IndexerModule
from app.runtime.hostports.filterrules import filter_rule_group_port
from app.runtime.hostports.siteresource import site_resource_port
from app.startup.hostport_initializer import configure_host_ports


@pytest.fixture(autouse=True)
def _restore_module_extension_ports():
    """用例结束后恢复组合根注册的实现，避免影响其它用例。"""
    yield
    configure_host_ports()


def test_site_resource_port_raises_clear_error_when_not_registered():
    """站点资源端口未注册时应给出可定位的报错。"""
    site_resource_port.reset()
    with pytest.raises(RuntimeError, match="site_resource"):
        site_resource_port.resolve()


def test_filter_rule_group_port_raises_clear_error_when_not_registered():
    """规则组端口未注册时应给出可定位的报错。"""
    filter_rule_group_port.reset()
    with pytest.raises(RuntimeError, match="filter_rule_group"):
        filter_rule_group_port.resolve()


def test_configure_host_ports_registers_working_site_resource():
    """组合根注册后站点资源端口应解析到可正常调用的实现。"""
    configure_host_ports()

    site_resource = site_resource_port.resolve()

    assert site_resource.get_indexer("does-not-exist.example") is None
    assert site_resource.get_indexers() == []


def test_configure_host_ports_registers_working_filter_rule_group():
    """组合根注册后规则组端口应解析到可正常调用的实现。"""
    configure_host_ports()

    filter_rule_group = filter_rule_group_port.resolve()

    assert filter_rule_group.get_custom_rules() == []
    assert filter_rule_group.get_rule_group_by_media() == []


class _FakeSiteResource:
    """索引扩展集成测试用的站点资源替身。"""

    def __init__(self, indexers):
        self._indexers = indexers

    def get_indexers(self):
        return self._indexers

    def get_indexer(self, domain):
        return next((item for item in self._indexers if item.get("domain") == domain), None)

    def check(self, domain):  # noqa: ARG002
        return False, ""


def test_indexer_module_test_consumes_registered_site_resource_port():
    """索引扩展的 test() 应经端口取用注册的站点资源实现。"""
    site_resource_port.register(lambda: _FakeSiteResource([{"domain": "demo"}]))

    ok, msg = IndexerModule().test()

    assert (ok, msg) == (True, "")


def test_indexer_module_test_reports_no_sites_when_provider_returns_empty():
    """站点资源实现返回空列表时索引扩展应报告未配置站点。"""
    site_resource_port.register(lambda: _FakeSiteResource([]))

    ok, msg = IndexerModule().test()

    assert ok is False
    assert msg


class _FakeFilterRuleGroupProvider:
    """过滤扩展集成测试用的规则组仓库替身。"""

    def __init__(self, groups=None, custom_rules=None):
        self._groups = groups or []
        self._custom_rules = custom_rules or []

    def get_custom_rules(self):
        return self._custom_rules

    def get_rule_group_by_media(self, media=None, group_names=None):  # noqa: ARG002
        return self._groups


def test_filter_module_init_resolves_rule_group_port():
    """过滤扩展构造时应经端口取用注册的规则组仓库实现。"""
    fake_rule_group_provider = _FakeFilterRuleGroupProvider()
    filter_rule_group_port.register(lambda: fake_rule_group_provider)

    module = FilterModule()

    assert module.rulehelper is fake_rule_group_provider


def test_filter_module_init_module_merges_custom_rules_into_builtin_set():
    """过滤扩展 init_module 应取用领域内置规则集，并叠加自定义规则。"""
    filter_rule_group_port.register(lambda: _FakeFilterRuleGroupProvider())

    with patch("app.modules.filter.get_builtin_rule_set", return_value={"X": {"include": ["x"]}}):
        module = FilterModule()
        module.init_module()

    assert module.builtin_rule_set == {"X": {"include": ["x"]}}
    assert module.rule_set["X"] == {"include": ["x"]}


def test_filter_module_filter_torrents_uses_domain_rule_expression_parser():
    """过滤扩展的 Python 兜底路径应使用领域层规则表达式解析器。"""
    from app.domain.context import TorrentInfo

    filter_rule_group_port.register(lambda: _FakeFilterRuleGroupProvider())

    with patch("app.modules.filter.get_builtin_rule_set", return_value={"KEEP": {"include": ["Movie"]}}):
        module = FilterModule()
        module.init_module()
    module.rulehelper = _FakeFilterRuleGroupProvider(
        groups=[type("Group", (), {"name": "test", "rule_string": "KEEP"})()]
    )
    torrent = TorrentInfo(title="Movie", description="")

    with patch("app.modules.filter.rust_accel.is_enabled", return_value=False):
        filtered = module.filter_torrents(rule_groups=["test"], torrent_list=[torrent])

    assert filtered == [torrent]
