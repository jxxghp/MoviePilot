"""架构与启动性能基线脚本的 CLI 行为测试。"""

import json
from pathlib import Path

import pytest

from scripts.architecture import baseline as architecture_baseline
from scripts.startup import performance as startup_performance


def _performance_sample(*, loaded_module_count: int = 10) -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-08-21T00:00:00+00:00",
        "platform": "test",
        "python": "3.12",
        "repeat": 1,
        "targets": {
            "app.factory": {
                "loaded_module_count": loaded_module_count,
                "max_ms": 100.0,
                "median_ms": 90.0,
                "min_ms": 80.0,
                "samples_ms": [90.0],
            }
        },
        "lifecycle": {
            "scope": "isolated no-op",
            "modes": {
                "normal": {
                    "enabled_component_count": 2,
                    "samples": [
                        {
                            "threads_before": 1,
                            "threads_after": 1,
                            "tasks_before": 1,
                            "tasks_after": 1,
                            "database_connections_started": 0,
                        }
                    ],
                }
            },
        },
    }


def test_architecture_legacy_action_requires_scope(capsys):
    """旧操作未明确宿主或插件范围时必须拒绝执行。"""
    with pytest.raises(SystemExit) as error:
        architecture_baseline.parse_args(["--check"])

    assert error.value.code == 2
    assert "必须同时指定 --scope" in capsys.readouterr().err


def test_architecture_legacy_action_maps_to_scoped_operation(capsys):
    """兼容期旧参数应提示弃用并映射到唯一范围。"""
    args = architecture_baseline.parse_args(["--check", "--scope", "host"])

    assert args.check_host is True
    assert "请改用 --check-host" in capsys.readouterr().err


def test_architecture_plugin_action_requires_repository(capsys):
    """插件基线操作缺少独立插件仓时必须在扫描前失败。"""
    with pytest.raises(SystemExit) as error:
        architecture_baseline.parse_args(["--check-plugins"])

    assert error.value.code == 2
    assert "必须指定 --plugin-repo" in capsys.readouterr().err


def test_architecture_diagnostics_only_support_host_check(capsys):
    """源码位置诊断不得与写操作或插件扫描混用。"""
    with pytest.raises(SystemExit) as error:
        architecture_baseline.parse_args(["--write-host", "--diagnostics"])

    assert error.value.code == 2
    assert "只能与 --check-host" in capsys.readouterr().err


def test_runtime_semantics_ignore_line_changes_but_keep_call_count(tmp_path: Path):
    """旧 fixture 的行号变化不影响门禁，重复调用次数仍属于语义。"""
    baseline_path = tmp_path / "runtime-contract-baseline.json"
    old_value = {
        "schema_version": 1,
        "run_module": {
            "method_count": 1,
            "call_count": 2,
            "dynamic_call_count": 0,
            "methods": {
                "search": [
                    {"caller": "app.chain.search", "line": 10, "mode": "sync"},
                    {"caller": "app.chain.search", "line": 11, "mode": "sync"},
                ]
            },
            "dynamic_calls": [],
        },
        "events": {
            "event_count": 1,
            "producer_count": 1,
            "consumer_count": 0,
            "events": {
                "EventType.NoticeMessage": {
                    "producers": [{"caller": "app.chain.message", "line": 20}],
                    "consumers": [],
                }
            },
            "dynamic_producers": [],
            "dynamic_consumers": [],
        },
        "sdk_exports": {},
        "compat_manifest": {},
    }
    moved_value = json.loads(json.dumps(old_value))
    moved_value["run_module"]["methods"]["search"][0]["line"] = 1000
    moved_value["run_module"]["methods"]["search"][1]["line"] = 1001
    moved_value["events"]["events"]["EventType.NoticeMessage"]["producers"][0][
        "line"
    ] = 2000

    old_semantic = architecture_baseline.semantic_baseline(baseline_path, old_value)
    moved_semantic = architecture_baseline.semantic_baseline(
        baseline_path,
        moved_value,
    )

    assert old_semantic == moved_semantic
    assert old_semantic["run_module"]["methods"]["search"] == [
        {"caller": "app.chain.search", "mode": "sync", "count": 2}
    ]


def test_plugin_provenance_does_not_participate_in_semantic_gate(tmp_path: Path):
    """插件仓 revision 和源码摘要变化不应伪装成 ABI 变化。"""
    baseline_path = tmp_path / "official-plugin-baseline.json"
    first = {
        "schema_version": 3,
        "scope": {"repository": "MoviePilot-Plugins", "roots": ["plugins.v3"]},
        "provenance": {
            "head": "a" * 40,
            "python_file_count": 1,
            "source_sha256": "a" * 64,
        },
        "imports": {},
        "hooks": {},
        "api_routes": {},
    }
    second = json.loads(json.dumps(first))
    second["provenance"] = {
        "head": "b" * 40,
        "python_file_count": 2,
        "source_sha256": "b" * 64,
    }

    assert architecture_baseline.semantic_baseline(
        baseline_path,
        first,
    ) == architecture_baseline.semantic_baseline(baseline_path, second)


def test_plugin_v2_fixture_migrates_before_semantic_comparison(tmp_path: Path):
    """旧插件 fixture 应在内存中迁移，不能因 schema 排列变化误报 ABI。"""
    baseline_path = tmp_path / "official-plugin-baseline.json"
    old_value = {
        "schema_version": 2,
        "source": {
            "repository": "MoviePilot-Plugins",
            "head": "a" * 40,
            "roots": ["plugins.v2", "plugins.v3"],
            "python_file_count": 1,
            "source_sha256": "a" * 64,
        },
        "imports": {"app.sdk.logging": {"file_count": 1, "files": ["x.py"]}},
        "hooks": {},
        "api_routes": {},
    }
    new_value = {
        "schema_version": 3,
        "scope": {
            "repository": "MoviePilot-Plugins",
            "roots": ["plugins.v2", "plugins.v3"],
        },
        "provenance": {
            "head": "b" * 40,
            "python_file_count": 2,
            "source_sha256": "b" * 64,
        },
        "imports": old_value["imports"],
        "hooks": {},
        "api_routes": {},
    }

    assert architecture_baseline.semantic_baseline(
        baseline_path,
        old_value,
    ) == architecture_baseline.semantic_baseline(baseline_path, new_value)


def test_architecture_write_host_only_updates_host_files(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    """宿主写操作不得连带覆盖官方插件 fixture。"""
    dependency_path = tmp_path / "dependency.json"
    runtime_path = tmp_path / "runtime.json"
    plugin_path = tmp_path / "plugin.json"
    monkeypatch.setattr(
        architecture_baseline,
        "DEPENDENCY_BASELINE_PATH",
        dependency_path,
    )
    monkeypatch.setattr(
        architecture_baseline,
        "RUNTIME_BASELINE_PATH",
        runtime_path,
    )
    monkeypatch.setattr(architecture_baseline, "PLUGIN_BASELINE_PATH", plugin_path)
    monkeypatch.setattr(
        architecture_baseline,
        "collect_dependency_baseline",
        lambda: {"scope": "host-dependency"},
    )
    monkeypatch.setattr(
        architecture_baseline,
        "collect_runtime_baseline",
        lambda: {"scope": "host-runtime"},
    )

    assert architecture_baseline.main(["--write-host"]) == 0

    assert json.loads(dependency_path.read_text()) == {"scope": "host-dependency"}
    assert json.loads(runtime_path.read_text()) == {"scope": "host-runtime"}
    assert not plugin_path.exists()
    output = capsys.readouterr().out
    assert "即将写入" in output
    assert "dependency.json" in output
    assert "runtime.json" in output


def test_architecture_write_plugins_only_updates_plugin_file(
    tmp_path: Path,
    monkeypatch,
):
    """插件写操作不得修改宿主依赖和运行契约 fixture。"""
    plugin_repo = tmp_path / "MoviePilot-Plugins"
    (plugin_repo / "plugins.v2").mkdir(parents=True)
    (plugin_repo / "plugins.v3").mkdir()
    dependency_path = tmp_path / "dependency.json"
    runtime_path = tmp_path / "runtime.json"
    plugin_path = tmp_path / "plugin.json"
    monkeypatch.setattr(
        architecture_baseline,
        "DEPENDENCY_BASELINE_PATH",
        dependency_path,
    )
    monkeypatch.setattr(
        architecture_baseline,
        "RUNTIME_BASELINE_PATH",
        runtime_path,
    )
    monkeypatch.setattr(architecture_baseline, "PLUGIN_BASELINE_PATH", plugin_path)

    assert architecture_baseline.main(
        ["--write-plugins", "--plugin-repo", str(plugin_repo)]
    ) == 0

    assert plugin_path.is_file()
    assert not dependency_path.exists()
    assert not runtime_path.exists()


def test_performance_default_print_does_not_write_fixture(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    """性能脚本无操作参数时只打印采样，不得创建输出文件。"""
    output = tmp_path / "performance.json"
    sample = _performance_sample()
    monkeypatch.setattr(startup_performance, "DEFAULT_OUTPUT", output)
    monkeypatch.setattr(startup_performance, "collect_baseline", lambda _repeat: sample)

    assert startup_performance.main(["--repeat", "1"]) == 0

    assert not output.exists()
    assert json.loads(capsys.readouterr().out) == sample


def test_performance_check_is_read_only(tmp_path: Path, monkeypatch, capsys):
    """性能检查应使用现有 fixture 且保持文件内容不变。"""
    output = tmp_path / "performance.json"
    sample = _performance_sample()
    output.write_text(json.dumps(sample), encoding="utf-8")
    content_before = output.read_bytes()
    monkeypatch.setattr(startup_performance, "collect_baseline", lambda _repeat: sample)

    assert startup_performance.main(
        ["--check", "--repeat", "1", "--output", str(output)]
    ) == 0

    assert output.read_bytes() == content_before
    assert "检查通过" in capsys.readouterr().out


def test_performance_write_requires_explicit_action(tmp_path: Path, monkeypatch):
    """只有显式 write 操作才允许写入性能 fixture。"""
    output = tmp_path / "performance.json"
    sample = _performance_sample()
    monkeypatch.setattr(startup_performance, "collect_baseline", lambda _repeat: sample)

    assert startup_performance.main(
        ["--write", "--repeat", "1", "--output", str(output)]
    ) == 0

    assert json.loads(output.read_text(encoding="utf-8")) == sample


def test_performance_check_reports_loaded_module_drift():
    """稳定的冷导入模块数量变化必须产生可诊断失败。"""
    expected = _performance_sample(loaded_module_count=10)
    actual = _performance_sample(loaded_module_count=11)

    errors = startup_performance.check_baseline(expected, actual)

    assert errors == ["app.factory 加载模块数变化：10 -> 11"]
