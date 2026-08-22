"""候选种子分析器契约、合取组合与两条插件接入路径的回归测试。"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.application.orchestration.ports.search import SearchPorts
from app.domain.context import TorrentInfo
from app.modules.filter import FilterModule
from app.runtime.extensions.projection.dispatcher import ModuleInvocationDispatcher
from app.runtime.hostports.torrentanalysis import torrent_analysis_port
from app.schemas.filter import TorrentVerdict


class _RuleHelper:
    """
    过滤模块测试用的轻量规则仓库，避免依赖真实系统配置。
    """

    def __init__(self, groups):
        """
        保存测试规则组。
        """
        self._groups = groups

    def get_rule_group_by_media(self, media=None, group_names=None):  # noqa: ARG002
        """
        按名称返回测试规则组。
        """
        if not group_names:
            return self._groups
        return [group for group in self._groups if group.name in group_names]


class _ModuleCatalog:
    """
    按方法名投影固定宿主模块的内存目录。
    """

    def __init__(self, modules):
        """
        保存参与分发的测试模块。
        """
        self._modules = modules

    def get_running_modules(self, method: str):
        """
        返回实现了指定方法的测试模块。
        """
        return [module for module in self._modules if callable(getattr(module, method, None))]

    def providers_for(self, method: str) -> tuple:
        """
        返回实现了指定方法的测试模块，模拟能力索引命中。
        """
        return tuple(self.get_running_modules(method))


class _PluginCatalog:
    """
    提供固定插件方法表的内存目录。
    """

    def __init__(self, modules: dict):
        """
        保存插件方法表快照。
        """
        self._modules = modules

    def get_plugin_modules(self) -> dict:
        """
        返回当前插件方法表快照。
        """
        return self._modules


def _build_filter_module(rule_string: str, rule_set: dict) -> FilterModule:
    """
    构造绑定轻量规则仓库的过滤模块。
    """
    module = FilterModule()
    module.rulehelper = _RuleHelper(
        [SimpleNamespace(name="test", rule_string=rule_string)]
    )
    module.rule_set = rule_set
    return module


def _build_ports(module: FilterModule, plugins: dict = None) -> SearchPorts:
    """
    以真实分发器构造搜索能力端口，宿主侧只有被测过滤模块。
    """
    dispatcher = ModuleInvocationDispatcher(
        module_catalog=_ModuleCatalog([module]),
        plugin_catalog=_PluginCatalog(plugins or {}),
        plugin_error_handler=Mock(),
        system_error_handler=Mock(),
        rate_limit_handler=Mock(),
    )
    return SearchPorts(dispatcher)


def _register_analysis_dispatch(module: FilterModule, plugins: dict = None) -> SearchPorts:
    """
    把真实多播分发注入分析能力端口，供过滤模块收集全部分析器判定。
    """
    ports = _build_ports(module, plugins)
    torrent_analysis_port.register(lambda: ports)
    return ports


def _verdicts(*specs) -> list:
    """
    按 (是否通过, 排序权重) 构造插件分析器的判定列表。
    """
    return [
        TorrentVerdict(
            analyzer="demo-analyzer",
            passed=passed,
            reason="测试通过" if passed else "测试否决",
            order=order,
        )
        for passed, order in specs
    ]


def _hdr_module() -> FilterModule:
    """
    构造使用 HDR 优先级规则的过滤模块。
    """
    return _build_filter_module(
        rule_string="HDR & !BLU > DV",
        rule_set={
            "HDR": {"include": "HDR"},
            "DV": {"include": "DOVI"},
            "BLU": {"include": "BluRay"},
        },
    )


def _hdr_torrents() -> list:
    """
    构造覆盖两级优先级和一个被规则排除项的候选列表。
    """
    return [
        TorrentInfo(title="Movie HDR WEB-DL", description=""),
        TorrentInfo(title="Movie DOVI", description=""),
        TorrentInfo(title="Movie HDR BluRay", description=""),
    ]


@pytest.fixture(autouse=True)
def reset_torrent_analysis_port():
    """
    用例结束后清除分析能力端口，避免分发替身泄漏到其它用例。
    """
    yield
    torrent_analysis_port.reset()


def test_builtin_analyzer_reports_verdict_for_every_candidate():
    """内置分析器应给出与候选列表等长、含依据和排序权重的判定。"""
    module = _hdr_module()
    torrents = _hdr_torrents()

    verdicts = module.analyze_torrent_candidates(rule_groups=["test"], torrent_list=torrents)

    assert [verdict.passed for verdict in verdicts] == [True, True, False]
    assert [verdict.order for verdict in verdicts] == [100, 99, None]
    assert {verdict.analyzer for verdict in verdicts} == {FilterModule.ANALYZER_ID}
    assert all(verdict.reason for verdict in verdicts)
    assert "test" in verdicts[2].reason


def test_builtin_analyzer_stays_out_without_applicable_rule_group():
    """没有规则组名称时内置分析器不参与判定。"""
    module = _hdr_module()

    assert module.analyze_torrent_candidates(rule_groups=[], torrent_list=_hdr_torrents()) is None


def test_filter_torrents_keeps_candidates_when_no_analyzer_participates():
    """无人给出判定时候选原样返回，且不改动排序权重。"""
    module = _hdr_module()
    torrents = _hdr_torrents()
    _register_analysis_dispatch(module)

    filtered = module.filter_torrents(rule_groups=[], torrent_list=torrents)

    assert filtered is torrents
    assert [torrent.pri_order for torrent in torrents] == [0, 0, 0]


def test_builtin_analyzer_alone_keeps_filter_results_and_priority():
    """只有内置分析器在场时，幸存者与优先级与既有过滤结果一致。"""
    module = _hdr_module()
    torrents = _hdr_torrents()
    _register_analysis_dispatch(module)

    filtered = module.filter_torrents(rule_groups=["test"], torrent_list=torrents)

    assert torrents[:2] == filtered
    assert filtered[0].pri_order == 100
    assert filtered[1].pri_order == 99


def test_plugin_analyzer_veto_rejects_candidate_accepted_by_builtin():
    """插件分析器否决的候选整体否决，内置分析器通过也不例外。"""
    module = _hdr_module()
    torrents = _hdr_torrents()
    plugin_analyzer = Mock(return_value=_verdicts((False, None), (True, None), (True, None)))
    _register_analysis_dispatch(
        module,
        {("DemoPlugin", "演示插件"): {"analyze_torrent_candidates": plugin_analyzer}},
    )

    filtered = module.filter_torrents(rule_groups=["test"], torrent_list=torrents)

    assert filtered == [torrents[1]]
    assert filtered[0].pri_order == 99
    plugin_analyzer.assert_called_once()


def test_builtin_veto_still_rejects_candidate_passed_by_plugin_analyzer():
    """内置分析器否决的候选整体否决，插件分析器全部放行也不例外。"""
    module = _hdr_module()
    torrents = _hdr_torrents()
    _register_analysis_dispatch(
        module,
        {
            ("DemoPlugin", "演示插件"): {
                "analyze_torrent_candidates": lambda **_kwargs: _verdicts(
                    (True, None), (True, None), (True, None)
                )
            }
        },
    )

    filtered = module.filter_torrents(rule_groups=["test"], torrent_list=torrents)

    assert filtered == torrents[:2]


def test_rejected_candidate_traces_back_to_the_vetoing_analyzer():
    """否决日志应记录否决者标识与判定依据，可追溯到是谁否决的。"""
    module = _hdr_module()
    torrents = _hdr_torrents()
    _register_analysis_dispatch(
        module,
        {
            ("DemoPlugin", "演示插件"): {
                "analyze_torrent_candidates": lambda **_kwargs: _verdicts(
                    (False, None), (True, None), (True, None)
                )
            }
        },
    )

    with patch("app.modules.filter.logger.debug") as log_debug:
        module.filter_torrents(rule_groups=["test"], torrent_list=torrents)

    rejections = [
        call.args[0]
        for call in log_debug.call_args_list
        if call.args and "被过滤" in str(call.args[0])
    ]
    assert any("demo-analyzer 测试否决" in message for message in rejections)
    assert any(f"{FilterModule.ANALYZER_ID} 不匹配过滤规则组 test" in message for message in rejections)


def test_order_uses_first_analyzer_that_provides_one():
    """排序权重取分发顺序中首个给出取值的分析器，插件分析器先于内置分析器。"""
    module = _hdr_module()
    torrents = _hdr_torrents()
    _register_analysis_dispatch(
        module,
        {
            ("DemoPlugin", "演示插件"): {
                "analyze_torrent_candidates": lambda **_kwargs: _verdicts(
                    (True, 7), (True, None), (True, 7)
                )
            }
        },
    )

    filtered = module.filter_torrents(rule_groups=["test"], torrent_list=torrents)

    assert filtered == torrents[:2]
    # 首个给出取值的是插件分析器
    assert filtered[0].pri_order == 7
    # 插件未给出取值时回落到内置分析器
    assert filtered[1].pri_order == 99


def test_misaligned_analyzer_verdicts_are_ignored():
    """判定数量与候选数量不一致的分析器不参与组合，避免判定错位。"""
    module = _hdr_module()
    torrents = _hdr_torrents()
    _register_analysis_dispatch(
        module,
        {
            ("DemoPlugin", "演示插件"): {
                "analyze_torrent_candidates": lambda **_kwargs: _verdicts((False, None))
            }
        },
    )

    with patch("app.modules.filter.logger.warn") as log_warn:
        filtered = module.filter_torrents(rule_groups=["test"], torrent_list=torrents)

    assert filtered == torrents[:2]
    log_warn.assert_called_once()


def test_plugin_filter_torrents_replaces_builtin_filtering():
    """插件实现 filter_torrents 时整体接管过滤，内置分析器不再参与。"""
    module = _hdr_module()
    torrents = _hdr_torrents()
    ports = _build_ports(
        module,
        {
            ("DemoPlugin", "演示插件"): {
                "filter_torrents": lambda **kwargs: kwargs["torrent_list"][-1:]
            }
        },
    )

    with patch.object(module, "analyze_torrent_candidates") as builtin_analyzer:
        filtered = ports.filter_torrents(rule_groups=["test"], torrent_list=torrents)

    assert filtered == torrents[-1:]
    builtin_analyzer.assert_not_called()


def test_builtin_filter_torrents_answers_when_no_plugin_claims_it():
    """无插件认领 filter_torrents 时仍由内置规则引擎按单播作答。"""
    module = _hdr_module()
    torrents = _hdr_torrents()
    ports = _build_ports(module)
    _register_analysis_dispatch(module)

    filtered = ports.filter_torrents(rule_groups=["test"], torrent_list=torrents)

    assert filtered == torrents[:2]
    assert filtered[0].pri_order == 100
    assert filtered[1].pri_order == 99


def test_analyze_torrent_candidates_multicast_collects_every_analyzer():
    """分析能力按多播收集，内置与插件分析器的判定都进入结果。"""
    module = _hdr_module()
    torrents = _hdr_torrents()
    ports = _build_ports(
        module,
        {
            ("DemoPlugin", "演示插件"): {
                "analyze_torrent_candidates": lambda **_kwargs: _verdicts(
                    (True, None), (True, None), (True, None)
                )
            }
        },
    )

    verdict_groups = ports.analyze_torrent_candidates(rule_groups=["test"], torrent_list=torrents)

    assert [group[0].analyzer for group in verdict_groups] == [
        "demo-analyzer",
        FilterModule.ANALYZER_ID,
    ]
