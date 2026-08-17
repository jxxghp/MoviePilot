import json
import subprocess
import sys
from pathlib import Path

from app.schemas.types import ChainEventType, EventType


PROJECT_ROOT = Path(__file__).parents[1]
BASELINE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "architecture"


def test_architecture_contract_baselines_match_current_source():
    """宿主依赖图和公开运行契约变化必须显式刷新基线。"""
    result = subprocess.run(
        [sys.executable, "scripts/architecture/baseline.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_official_plugin_baseline_records_external_source():
    """官方插件快照必须绑定独立仓提交，且不得引用宿主插件副本。"""
    baseline_path = BASELINE_ROOT / "official-plugin-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline["source"]["repository"] == "MoviePilot-Plugins"
    assert len(baseline["source"]["head"]) == 40
    assert baseline["source"]["roots"] == ["plugins.v2", "plugins.v3"]
    assert all(
        not path.startswith("app/plugins/")
        for contract in (*baseline["imports"].values(), *baseline["hooks"].values())
        for path in contract["files"]
    )
    assert all(
        not path.startswith("app/plugins/")
        for path in baseline["api_routes"]
    )


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
