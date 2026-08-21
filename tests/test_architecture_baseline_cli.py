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
