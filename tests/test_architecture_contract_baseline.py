import ast
import json
import os
import subprocess
import sys
from pathlib import Path

from app.schemas.types import ChainEventType, EventType

PROJECT_ROOT = Path(__file__).parents[1]
BASELINE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "architecture"


def test_official_plugin_baseline_records_external_source():
    """官方插件快照必须绑定独立仓提交，且不得引用宿主插件副本。"""
    baseline_path = BASELINE_ROOT / "official-plugin-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline["schema_version"] == 4
    assert baseline["scope"]["repository"] == "MoviePilot-Plugins"
    assert baseline["scope"]["roots"] == ["plugins.v2", "plugins.v3", "plugins"]
    assert baseline["scope"]["default_plugins"]
    assert len(baseline["provenance"]["head"]) == 40
    assert all(
        not path.startswith("app/plugins/")
        for contract in (
            *baseline["imports"].values(),
            *baseline["from_imports"].values(),
            *baseline["attribute_calls"].values(),
            *baseline["hooks"].values(),
        )
        for path in contract["files"]
    )
    assert all(
        not path.startswith("app/plugins/")
        for path in baseline["api_routes"]
    )
    assert {
        "app.agent.llm.LLMHelper",
        "app.agent.llm.helper.LLMHelper",
        "app.helper.llm.LLMHelper",
    } <= set(baseline["from_imports"])
    assert {
        "app.agent.llm.LLMHelper.get_llm",
        "app.agent.llm.helper.LLMHelper.test_current_settings",
        "app.helper.llm.LLMHelper.get_llm",
        "app.agent.tools.manager.moviepilot_tool_manager._load_tools",
    } <= set(baseline["attribute_calls"])


def test_dependency_baseline_records_nonempty_host_graph() -> None:
    """宿主依赖 fixture 不得因收集器提前返回而被静默写成空值。"""
    baseline_path = BASELINE_ROOT / "dependency-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline["schema_version"] == 3
    assert baseline["module_count"] == len(baseline["modules"])
    assert baseline["edge_count"] == len(baseline["edges"])
    assert baseline["module_count"] > 0
    assert baseline["edge_count"] > 0
    direct_imports = baseline["direct_adapter_imports"]
    assert direct_imports["count"] == len(direct_imports["edges"])
    assert sum(direct_imports["counts_by_source_root"].values()) == direct_imports["count"]
    assert set(direct_imports["counts_by_source_root"]) <= {
        "app.application",
        "app.chain",
    }
    assert direct_imports["source_count"] == len(direct_imports["sources"])
    assert direct_imports["target_count"] == len(direct_imports["targets"])
    direct_egress = baseline["direct_egress"]
    assert direct_egress["count"] == len(direct_egress["entries"])
    assert sum(direct_egress["counts_by_kind"].values()) == direct_egress["count"]
    assert set(direct_egress["counts_by_kind"]) == {
        "raw_transport",
        "network_sdk",
        "protocol_operation",
    }
    assert set(direct_egress["application_chain_counts"]) == {
        "app.application",
        "app.chain",
    }
    assert all("line" not in entry for entry in direct_egress["entries"])


def test_architecture_documents_match_generated_quality_metrics() -> None:
    """高漂移量化指标必须与生成 fixture 同步，不能在多份文档中分叉。"""
    dependency = json.loads(
        (BASELINE_ROOT / "dependency-baseline.json").read_text(encoding="utf-8")
    )
    ruff = json.loads(
        (BASELINE_ROOT / "ruff-baseline.json").read_text(encoding="utf-8")
    )
    mypy = json.loads(
        (BASELINE_ROOT / "mypy-baseline.json").read_text(encoding="utf-8")
    )
    coverage = json.loads(
        (BASELINE_ROOT / "coverage-baseline.json").read_text(encoding="utf-8")
    )
    runtime = json.loads(
        (BASELINE_ROOT / "runtime-contract-baseline.json").read_text(
            encoding="utf-8"
        )
    )
    overview = (PROJECT_ROOT / "docs" / "architecture-overview.md").read_text(
        encoding="utf-8"
    )
    checklist = (
        PROJECT_ROOT
        / "docs"
        / "architecture"
        / "optimization-checklist.md"
    ).read_text(encoding="utf-8")
    roadmap = (
        PROJECT_ROOT
        / "docs"
        / "architecture"
        / "refactor-roadmap.md"
    ).read_text(encoding="utf-8")
    edge_count = f"{dependency['edge_count']:,}"
    ruff_count = sum(
        count
        for diagnostics in ruff.values()
        for count in diagnostics.values()
    )
    mypy_count = sum(
        count
        for diagnostics in mypy.values()
        for count in diagnostics.values()
    )
    application_coverage = coverage["application"]["percent"]
    domain_coverage = coverage["domain"]["percent"]
    event_facts = runtime["event_facts"]

    assert edge_count in overview
    assert f"{dependency['module_count']} / {edge_count}" in checklist
    assert f"全量 mypy 历史债务 | {mypy_count:,} / {len(mypy)} 文件" in checklist
    assert f"Ruff 历史诊断 | {ruff_count}" in checklist
    assert f"Application {application_coverage:.2f}%" in checklist
    assert f"Domain {domain_coverage:.2f}%" in checklist
    if "**阶段状态：`CANCELLED`" in roadmap:
        assert "| S4-L5 Ruff 治理债务清零 | `CANCELLED` |" in roadmap
    else:
        assert f"当前受控 {ruff_count} 条诊断归零" in roadmap
    assert (
        f"当前宿主有 {event_facts['producer_call_count']} 个\n"
        f"生产调用，其中 {event_facts['static_producer_call_count']} 个静态解析为 "
        f"{event_facts['producer_event_reference_count']} 个事件引用"
    ) in overview
    assert (
        f"{event_facts['consumer_registration_count']} 个消费注册中 "
        f"{event_facts['static_consumer_count']} 个静态、"
        f"{event_facts['dynamic_consumer_count']} 个动态"
    ) in overview
    assert (
        f"{event_facts['producer_call_count']} 个 producer（"
        f"{event_facts['static_producer_call_count']} 静态、"
        f"{event_facts['dynamic_producer_count']} 动态）"
    ) in checklist
    assert (
        f"{event_facts['consumer_registration_count']} 个 consumer（"
        f"{event_facts['static_consumer_count']} 静态、"
        f"{event_facts['dynamic_consumer_count']} 动态）"
    ) in checklist


def test_official_discovery_plugins_explicitly_keep_host_page_envelope():
    """宿主探索页消费的官方插件 API 不得依赖动态路由隐式包装。"""
    baseline_path = BASELINE_ROOT / "official-plugin-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    routes = {
        (path, route["path"]): route
        for path, file_routes in baseline["api_routes"].items()
        for route in file_routes
    }

    expected_paths = {
        ("plugins.v3/imdbsource/__init__.py", "/imdb-discover"),
        ("plugins.v3/imdbsource/__init__.py", "/imdb-top-250"),
        ("plugins.v3/imdbsource/__init__.py", "/imdb-trending"),
        ("plugins.v3/imdbsource/__init__.py", "/trending"),
        ("plugins.v3/tvdbdiscover/__init__.py", "/tvdb_discover"),
    }
    for route_key in expected_paths:
        route = routes[route_key]
        assert route["response_model"] == "schemas.Response[List[schemas.MediaInfo]]"
        assert route["endpoint_return"] == "schemas.Response[List[schemas.MediaInfo]]"


def test_startup_performance_baseline_records_all_cold_import_targets():
    """启动性能基线必须包含关键入口的可比较冷导入采样。"""
    baseline_path = BASELINE_ROOT / "startup-performance-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline["schema_version"] == 2
    assert baseline["repeat"] >= 3
    assert set(baseline["targets"]) == {
        "app.startup.lifecycle",
        "app.factory",
        "app.main",
    }
    for contract in baseline["targets"].values():
        assert len(contract["samples_ms"]) == baseline["repeat"]
        assert contract["min_ms"] <= contract["median_ms"] <= contract["max_ms"]
        assert contract["loaded_app_module_count"] > 0


def test_runtime_contract_baseline_excludes_diagnostic_line_numbers():
    """运行契约 fixture 只保存稳定语义，源码位置必须按需诊断。"""
    baseline_path = BASELINE_ROOT / "runtime-contract-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline["schema_version"] == 3
    assert baseline["scope"] == {
        "repository": "MoviePilot",
        "roots": ["app"],
        "excluded": ["app/plugins"],
    }
    assert '"line"' not in json.dumps(baseline)


def test_transaction_debt_baseline_is_a_model_and_oper_ratchet() -> None:
    """事务 fixture 必须保持 Model 写装饰器归零，并冻结剩余查询债务。"""
    baseline_path = BASELINE_ROOT / "transaction-debt-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline["schema_version"] == 1
    assert baseline["model_decorators"]["count"] == 0
    assert sum(baseline["model_decorators"]["by_kind"].values()) == 0
    assert baseline["model_decorators"]["by_kind"]["db_update"] == 0
    assert baseline["model_decorators"]["by_kind"]["async_db_update"] == 0
    assert baseline["model_transaction_calls"] == {"count": 0, "calls": []}
    assert baseline["model_session_factories"] == {"count": 0, "calls": []}
    assert baseline["oper_transaction_calls"] == {"count": 0, "calls": []}
    assert baseline["oper_session_factories"] == {"count": 0, "calls": []}


def test_host_oper_does_not_call_base_implicit_write_wrappers() -> None:
    """宿主 Oper 不得重新借 Base 兼容写方法隐式提交调用方事务。"""
    implicit_methods = {
        "create",
        "async_create",
        "update",
        "async_update",
        "delete",
        "async_delete",
        "truncate",
        "async_truncate",
    }
    violations = []
    for path in (PROJECT_ROOT / "app" / "db" / "oper").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in implicit_methods or not node.args:
                continue
            first_argument = node.args[0]
            if (
                isinstance(first_argument, ast.Attribute)
                and isinstance(first_argument.value, ast.Name)
                and first_argument.value.id == "self"
                and first_argument.attr == "_db"
            ):
                violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")

    assert violations == []


def test_configuration_debt_baseline_tracks_canonical_direct_access() -> None:
    """配置基线必须把零债务与固定基础设施边界分开冻结。"""
    baseline_path = BASELINE_ROOT / "configuration-debt-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline["schema_version"] == 2
    assert baseline["scope"]["excluded"] == [
        "app/plugins",
        "app/sdk",
        "app/runtime/compat",
        "app/testing",
    ]
    assert baseline["settings_imports"]["count"] == len(
        baseline["settings_imports"]["files"]
    )
    assert baseline["system_config_oper_constructions"]["count"] == len(
        baseline["system_config_oper_constructions"]["calls"]
    )
    assert baseline["settings_imports"] == {"count": 0, "files": []}
    assert baseline["system_config_oper_constructions"] == {
        "count": 0,
        "calls": [],
    }
    assert baseline["foundational_settings_boundaries"] == {
        "count": 0,
        "entries": [],
    }
    assert baseline["composition_root_oper_boundaries"] == {
        "count": 0,
        "entries": [],
    }


def test_startup_performance_baseline_records_normal_and_safe_lifecycle_resources():
    """非功能基线必须同时记录正常/安全模式和隔离资源增量。"""
    baseline_path = BASELINE_ROOT / "startup-performance-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    lifecycle = baseline["lifecycle"]

    assert "no-op" in lifecycle["scope"]
    assert set(lifecycle["modes"]) == {"normal", "safe"}
    normal = lifecycle["modes"]["normal"]
    safe = lifecycle["modes"]["safe"]
    assert normal["enabled_component_count"] > safe["enabled_component_count"]
    for mode in (normal, safe):
        assert "后台任务登记器" in mode["enabled_components"]
        assert mode["enabled_component_count"] == len(mode["enabled_components"])
        assert len(mode["samples"]) == baseline["repeat"]
        for sample in mode["samples"]:
            assert sample["threads_after"] == sample["threads_before"]
            assert sample["tasks_after"] == sample["tasks_before"]
            assert sample["database_connections_started"] == 0
            assert sample["stage_ms"]


def test_schema_export_manifest_matches_current_modules():
    """Schema 公开符号或冲突来源变化必须显式刷新生成清单。"""
    result = subprocess.run(
        [sys.executable, "scripts/schema/exports.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_schema_root_import_does_not_eagerly_load_schema_graph():
    """仅导入 schema 根包时不得加载任一业务 schema 子模块。"""
    script = """
import sys
import app.schemas

loaded = sorted(
    name
    for name in sys.modules
    if name.startswith('app.schemas.') and name != 'app.schemas.exports'
)
assert not loaded, loaded
assert len(app.schemas.__all__) >= 400
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_agent_policy_root_import_does_not_eagerly_load_policy_submodules():
    """策略公共入口惰性导出，避免 orchestrator、registry 和 sanitizer 形成导入环。"""
    script = """
import sys
import app.agent.policy

assert not any(
    name.startswith('app.agent.policy.')
    for name in sys.modules
)
from app.agent.policy import ToolOrigin, sanitize_for_host

assert ToolOrigin.AGENT_API.value == 'agent_api'
assert sanitize_for_host({'token': 'secret'}) == {'token': '***'}
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_doctor_and_monitor_roots_are_lazy_identity_preserving_facades(tmp_path):
    """诊断和监控包根不得预载实现，旧路径仍需返回同一公开对象。"""
    script = """
import sys
import app.doctor
import app.monitor

assert not any(name.startswith('app.doctor.') for name in sys.modules)
assert not any(name.startswith('app.monitor.') for name in sys.modules)

# 独立子进程不经过 pytest 引导，触发实现加载前必须隔离站点原生制品。
from app.testing.bootstrap import ensure_sites_stub
ensure_sites_stub()

from app.doctor import DoctorRunner, run_doctor
from app.doctor.runner import DoctorRunner as DirectDoctorRunner
from app.monitor import LocalDirectoryWatcher, Monitor
from app.monitor.monitor import Monitor as DirectMonitor
from app.monitor.watcher import LocalDirectoryWatcher as DirectWatcher

assert DoctorRunner is DirectDoctorRunner
assert Monitor is DirectMonitor
assert LocalDirectoryWatcher is DirectWatcher
assert callable(run_doctor)
"""
    env = {**os.environ, "CONFIG_DIR": str(tmp_path / "config")}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_split_host_module_roots_keep_manifest_entrypoint_identity():
    """迁入 module.py 的宿主模块须保持 manifest 包级入口和历史反射路径。"""
    script = """
from importlib import import_module
import sys

contracts = (
    ('app.modules.qqbot', 'QQBotModule'),
    ('app.modules.telegram', 'TelegramModule'),
    ('app.modules.trimemedia', 'TrimeMediaModule'),
    ('app.modules.ugreen', 'UgreenModule'),
)
for package_name, symbol_name in contracts:
    package = import_module(package_name)
    implementation_name = f'{package_name}.module'
    assert implementation_name not in sys.modules
    public_class = getattr(package, symbol_name)
    direct_class = getattr(import_module(implementation_name), symbol_name)
    assert public_class is direct_class
    assert public_class.__module__ == package_name
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_event_contract_baseline_covers_every_public_event_enum() -> None:
    """统一事件事实快照必须覆盖全部枚举和真实生产、消费调用。"""
    baseline_path = BASELINE_ROOT / "runtime-contract-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    events = baseline["event_facts"]
    expected = {
        *(f"EventType.{member.name}" for member in EventType),
        *(f"ChainEventType.{member.name}" for member in ChainEventType),
    }

    assert set(events["event_index"]) == expected
    assert events["event_count"] == len(expected)
    assert events["producer_call_count"] == 93
    assert events["static_producer_call_count"] == 92
    assert events["dynamic_producer_count"] == 1
    assert events["invalid_producer_count"] == 0
    assert events["producer_event_reference_count"] == 94
    assert events["consumer_registration_count"] == 17
    assert events["static_consumer_count"] == 16
    assert events["dynamic_consumer_count"] == 1
    assert events["invalid_consumer_count"] == 0
    assert events["consumer_event_reference_count"] == 16
    assert events["fact_count"] == 110
    assert len({fact["fingerprint"] for fact in events["consumers"]}) == 17
    assert all(
        not fact["caller"].startswith("app.plugins")
        for fact in (*events["producers"], *events["consumers"])
    )
