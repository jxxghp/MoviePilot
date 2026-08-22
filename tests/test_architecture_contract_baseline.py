import ast
import json
import os
import subprocess
import sys
from pathlib import Path

from app.schemas.types import ChainEventType, EventType


PROJECT_ROOT = Path(__file__).parents[1]
BASELINE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "architecture"
# 插件配置、服务配置与第三方身份三张表上仍由模型自持事务的写方法。
# 清偿方向：写入交给对应 Oper 的 UoW，本名单随之缩短，直至为空。
MODEL_WRITE_DECORATOR_DEBT = {
    ("app/db/models/pluginconfig.py", "PluginConfig.async_delete_by_instance"),
    ("app/db/models/pluginconfig.py", "PluginConfig.async_delete_by_plugin"),
    ("app/db/models/pluginconfig.py", "PluginConfig.async_set_default_target"),
    ("app/db/models/pluginconfig.py", "PluginConfig.clear_default_target"),
    ("app/db/models/pluginconfig.py", "PluginConfig.delete_by_instance"),
    ("app/db/models/pluginconfig.py", "PluginConfig.delete_by_plugin"),
    ("app/db/models/pluginconfig.py", "PluginConfig.set_default_target"),
    ("app/db/models/serviceconfig.py", "ServiceConfig.clear_default_target"),
    ("app/db/models/serviceconfig.py", "ServiceConfig.delete_by_identity"),
    ("app/db/models/serviceconfig.py", "ServiceConfig.replace_capability"),
    ("app/db/models/serviceconfig.py", "ServiceConfig.set_default_target"),
    ("app/db/models/serviceconfig.py", "ServiceConfig.update_by_identity"),
    ("app/db/models/user_identity.py", "UserIdentity.async_delete_by_id"),
    ("app/db/models/user_identity.py", "UserIdentity.async_delete_by_user_id"),
    ("app/db/models/user_identity.py", "UserIdentity.delete_by_id"),
    ("app/db/models/user_identity.py", "UserIdentity.delete_by_user_id"),
}


def test_architecture_contract_baselines_match_current_source():
    """宿主依赖图和公开运行契约变化必须显式刷新基线。"""
    baseline_paths = (
        BASELINE_ROOT / "dependency-baseline.json",
        BASELINE_ROOT / "runtime-contract-baseline.json",
        BASELINE_ROOT / "transaction-debt-baseline.json",
        BASELINE_ROOT / "configuration-debt-baseline.json",
    )
    contents_before = {
        path: path.read_bytes()
        for path in baseline_paths
    }
    result = subprocess.run(
        [sys.executable, "scripts/architecture/baseline.py", "--check-host"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert {
        path: path.read_bytes()
        for path in baseline_paths
    } == contents_before


def test_official_plugin_baseline_records_external_source():
    """官方插件快照必须绑定独立仓提交，且不得引用宿主插件副本。"""
    baseline_path = BASELINE_ROOT / "official-plugin-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline["schema_version"] == 3
    assert baseline["scope"]["repository"] == "MoviePilot-Plugins"
    assert baseline["scope"]["roots"] == ["plugins.v2", "plugins.v3"]
    assert len(baseline["provenance"]["head"]) == 40
    assert all(
        not path.startswith("app/plugins/")
        for contract in (*baseline["imports"].values(), *baseline["hooks"].values())
        for path in contract["files"]
    )
    assert all(
        not path.startswith("app/plugins/")
        for path in baseline["api_routes"]
    )


def test_dependency_baseline_records_nonempty_host_graph() -> None:
    """宿主依赖 fixture 不得因收集器提前返回而被静默写成空值。"""
    baseline_path = BASELINE_ROOT / "dependency-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline["schema_version"] == 1
    assert baseline["module_count"] == len(baseline["modules"])
    assert baseline["edge_count"] == len(baseline["edges"])
    assert baseline["module_count"] > 0
    assert baseline["edge_count"] > 0


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

    assert baseline["repeat"] >= 3
    assert set(baseline["targets"]) == {
        "app.startup.lifecycle",
        "app.factory",
        "app.main",
    }
    for contract in baseline["targets"].values():
        assert len(contract["samples_ms"]) == baseline["repeat"]
        assert contract["min_ms"] <= contract["median_ms"] <= contract["max_ms"]
        assert contract["loaded_module_count"] > 0


def test_runtime_contract_baseline_excludes_diagnostic_line_numbers():
    """运行契约 fixture 只保存稳定语义，源码位置必须按需诊断。"""
    baseline_path = BASELINE_ROOT / "runtime-contract-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline["schema_version"] == 2
    assert '"line"' not in json.dumps(baseline)


def test_transaction_debt_baseline_is_a_model_and_oper_ratchet() -> None:
    """事务 fixture 必须冻结 Model 写装饰器的逐条名单和剩余查询债务。

    写装饰器让模型自己开事务并提交，绕过 Oper 侧的 UoW；除下列三个模型外的任何模型
    出现写装饰器，或这三个模型再多一条，都会让本断言转红。按名单而不是按条数冻结：
    条数拦不住「删一条旧的、补一条新的」，那种改动一条债没还，账面却看不出来。
    """
    baseline_path = BASELINE_ROOT / "transaction-debt-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    write_decorators = {
        (entry["file"], entry["method"])
        for entry in baseline["model_decorators"]["methods"]
        if entry["decorator"] in {"db_update", "async_db_update"}
    }

    assert baseline["schema_version"] == 1
    assert baseline["model_decorators"]["count"] == 159
    assert sum(baseline["model_decorators"]["by_kind"].values()) == 159
    assert write_decorators == MODEL_WRITE_DECORATOR_DEBT
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
    """配置债务基线必须排除插件兼容面，并冻结两个可下降的直接访问集合。"""
    baseline_path = BASELINE_ROOT / "configuration-debt-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline["schema_version"] == 1
    assert baseline["scope"]["excluded"] == [
        "app/plugins",
        "app/sdk",
        "app/runtime/compat",
    ]
    assert baseline["settings_imports"]["count"] == len(
        baseline["settings_imports"]["files"]
    )
    assert baseline["system_config_oper_constructions"]["count"] == len(
        baseline["system_config_oper_constructions"]["calls"]
    )


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

# CI 无 app.application.site.sites 资源模块，触发实现加载前先补 conftest 同源垫片；
# 独立子进程不经过 pytest 引导，必须在此显式安装，否则链式 import 会因缺模块失败。
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


def test_event_contract_baseline_covers_every_public_event_enum() -> None:
    """事件生产者/消费者快照必须覆盖全部广播和链式事件枚举。"""
    baseline_path = BASELINE_ROOT / "runtime-contract-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    events = baseline["events"]
    expected = {
        *(f"EventType.{member.name}" for member in EventType),
        *(f"ChainEventType.{member.name}" for member in ChainEventType),
    }

    assert set(events["events"]) == expected
    assert events["event_count"] == len(expected)
    assert events["producer_count"] > 0
    assert events["consumer_count"] > 0
