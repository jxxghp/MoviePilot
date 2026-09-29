"""结构复杂度门禁、行数观察与基线写保护的回归测试。"""

import json
import subprocess
import sys
from pathlib import Path
from textwrap import indent

import pytest

from scripts.architecture import complexity


def _write_source(root: Path, relative: str, source: str) -> Path:
    """写入隔离源码树，避免对真实宿主文件做变异。"""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _branches(count: int, name: str = "dispatch") -> str:
    """构造分支数可控的函数，以真实 Ruff 验证阈值边界。"""
    return f"def {name}(value):\n" + "".join(
        f"    if value == {index}:\n        return {index}\n" for index in range(count)
    )


def _nested(depth: int) -> str:
    """构造独立于物理行数的深层条件控制流。"""
    return "def nested(value):\n" + "".join(
        f"{'    ' * level}if value:\n" for level in range(1, depth + 1)
    ) + f"{'    ' * (depth + 1)}return value\n"


def test_structural_metrics_cover_all_host_packages_and_ignore_only_plugins(tmp_path: Path) -> None:
    """原来未受治的宿主目录同样拦截短而多分支的函数；插件副本除外。"""
    packages = (
        "adapters", "agent", "api/endpoints", "application", "chain", "db", "domain",
        "foundation", "modules", "runtime", "scheduler", "startup", "workflow", "sdk",
        "runtime/compat", "testing",
    )
    for package in packages:
        _write_source(tmp_path, f"app/{package}/sample.py", _branches(15, "__private"))
    _write_source(tmp_path, "app/plugins/sample/__init__.py", "invalid syntax !")
    metrics = complexity.collect_complexity(tmp_path)
    assert metrics["C901"] == {
        f"app/{package}/sample.py:__private": 16 for package in packages
    }
    assert metrics["PLR1702"] == {}


def test_complexity_limits_are_inclusive_and_nested_depth_is_independent(tmp_path: Path) -> None:
    """圈复杂度 15、嵌套 5 层允许，跨过任一阈值必须报告。"""
    path = _write_source(tmp_path, "app/sample.py", _branches(14) + _nested(5))
    assert complexity.collect_complexity(tmp_path) == {"C901": {}, "PLR1702": {}}
    path.write_text(_branches(15) + _nested(6), encoding="utf-8")
    assert complexity.collect_complexity(tmp_path) == {
        "C901": {"app/sample.py:dispatch": 16},
        "PLR1702": {"app/sample.py:nested": 6},
    }


def test_formatting_comments_and_docstrings_only_affect_advisory_size(tmp_path: Path) -> None:
    """增加文档、空行或换行不消耗结构预算，但仍可观察源码规模变化。"""
    path = _write_source(tmp_path, "app/sample.py", _branches(15))
    before = complexity.collect_complexity(tmp_path)
    formatted = _branches(15).replace("(value):", "(\n    value,\n):").replace(
        "    if value == 0:",
        '    """文档\n' + "    说明\n" * 160 + '    """\n\n    # 分支语义未变\n    if value == 0:',
    )
    path.write_text("\n\n" + formatted, encoding="utf-8")
    assert complexity.collect_complexity(tmp_path) == before
    assert complexity.collect_source_size(tmp_path)["method"]["app/sample.py:dispatch"] > 150
    path.write_text("def linear():\n" + "    value = 1\n" * 1100, encoding="utf-8")
    assert complexity.collect_complexity(tmp_path) == {"C901": {}, "PLR1702": {}}
    assert complexity.collect_source_size(tmp_path)["file"] == {"app/sample.py": 1101}


def test_nested_owners_and_redefinitions_do_not_share_budgets(tmp_path: Path) -> None:
    """Match、TryStar 内嵌套函数与同名重定义必须保留独立身份。"""
    source = (
        "class Runner:\n" + indent(_branches(15, "__hidden"), "    ")
        + "\ndef outer(value):\n    match value:\n        case 1:\n"
        + indent(_branches(15, "matched"), "            ")
        + "\ndef guarded():\n    try:\n        pass\n    except* ValueError:\n"
        + indent(_branches(15, "recovered"), "        ")
        + "\ndef local():\n    class Inner:\n"
        + indent(_branches(15, "method"), "        ")
        + "\nif enabled:\n" + indent(_branches(15, "repeated"), "    ")
        + "else:\n" + indent(_branches(16, "repeated"), "    ")
    )
    _write_source(tmp_path, "app/sample.py", source)
    metrics = complexity.collect_complexity(tmp_path)["C901"]
    for owner in ("Runner.__hidden", "outer.matched", "guarded.recovered", "local.Inner.method"):
        assert metrics[f"app/sample.py:{owner}"] == 16
    assert metrics["app/sample.py:repeated#1"] == 16
    assert metrics["app/sample.py:repeated#2"] == 17


def test_noqa_gitignore_and_local_ruff_config_cannot_hide_metrics(tmp_path: Path) -> None:
    """noqa、忽略文件或局部 Ruff 排除项不能绕过宿主复杂度门禁。"""
    _write_source(tmp_path, "app/sample.py", _branches(15).replace(
        "def dispatch(value):", "def dispatch(value):  # noqa: C901",
    ))
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("app/\n", encoding="utf-8")
    (tmp_path / "ruff.toml").write_text('exclude = ["app"]\n', encoding="utf-8")
    assert complexity.collect_complexity(tmp_path)["C901"] == {"app/sample.py:dispatch": 16}


def test_multiple_deep_blocks_use_the_functions_maximum_depth(tmp_path: Path) -> None:
    """同函数多个深层块取最大深度，嵌套函数的指标归属自己。"""
    source = _nested(6) + _nested(7).split("\n", 1)[1]
    source += "\ndef outer():\n" + indent(_nested(8), "    ")
    _write_source(tmp_path, "app/sample.py", source)
    assert complexity.collect_complexity(tmp_path)["PLR1702"] == {
        "app/sample.py:nested": 7, "app/sample.py:outer.nested": 8,
    }


def test_growth_cannot_be_offset_by_reduction_or_renaming() -> None:
    """同文件中删除旧热点不能抵消另一个函数变复杂或搬走后的新热点。"""
    baseline = {"C901": {"app/a.py:old": 20, "app/a.py:growing": 18}, "PLR1702": {}}
    current = {"C901": {"app/b.py:moved": 20, "app/a.py:growing": 19}, "PLR1702": {}}
    regressions, stale = complexity.classify_complexity(baseline, current)
    assert len(regressions) == 2
    assert any("growing 18->19" in item for item in regressions)
    assert any("moved 15->20" in item for item in regressions)
    assert len(stale) == 1 and "old" in stale[0]


@pytest.mark.parametrize("current", [{"C901": {"app/a.py:f": 16}, "PLR1702": {}}, {"C901": {}, "PLR1702": {}}])
def test_cli_requires_and_writes_lower_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: dict,
) -> None:
    """降低到阈值内或删除热点后必须收紧基线，后续检查才能通过。"""
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(complexity.baseline_payload({"C901": {"app/a.py:f": 20}, "PLR1702": {}})))
    monkeypatch.setattr(complexity, "collect_complexity", lambda: current)
    monkeypatch.setattr(sys, "argv", ["complexity.py", "--baseline", str(baseline)])
    assert complexity.main() == 1
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--write"])
    assert complexity.main() == 0
    assert complexity.read_baseline(baseline) == current
    monkeypatch.setattr(sys, "argv", sys.argv[:-1])
    assert complexity.main() == 0


def test_cli_refuses_to_overwrite_growth_and_keeps_diagnostic_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败时保存审查报告，但 write 绝不能把回退洗入已有基线。"""
    baseline, report = tmp_path / "baseline.json", tmp_path / "report.json"
    original = json.dumps(complexity.baseline_payload({"C901": {}, "PLR1702": {}}))
    baseline.write_text(original, encoding="utf-8")
    current = {"C901": {"app/a.py:f": 16}, "PLR1702": {}}
    monkeypatch.setattr(complexity, "collect_complexity", lambda: current)
    monkeypatch.setattr(complexity, "collect_source_size", lambda: {"file": {"app/a.py": 2000}})
    monkeypatch.setattr(sys, "argv", ["complexity.py", "--write", "--baseline", str(baseline), "--report", str(report)])
    assert complexity.main() == 1
    assert baseline.read_text(encoding="utf-8") == original
    assert json.loads(report.read_text(encoding="utf-8"))["metrics"] == current
    monkeypatch.setattr(sys, "argv", ["complexity.py", "--baseline", str(baseline), "--report", str(baseline)])
    assert complexity.main() == 1
    assert baseline.read_text(encoding="utf-8") == original


def test_cli_missing_baseline_requires_explicit_initialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺失基线不能默认为成功，首次建账必须显式执行 write。"""
    baseline = tmp_path / "new.json"
    monkeypatch.setattr(complexity, "collect_complexity", lambda: {"C901": {}, "PLR1702": {}})
    monkeypatch.setattr(sys, "argv", ["complexity.py", "--baseline", str(baseline)])
    assert complexity.main() == 1
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--write"])
    assert complexity.main() == 0
    assert complexity.read_baseline(baseline) == {"C901": {}, "PLR1702": {}}


@pytest.mark.parametrize("payload", [
    {"api_endpoint": {}, "application_public": {}, "chain_public": {}},
    {"schema_version": 3, "thresholds": {"C901": 20, "PLR1702": 5}, "metrics": {}},
    complexity.baseline_payload({"C901": {"app/a.py:f": True}, "PLR1702": {}}),
    complexity.baseline_payload({"C901": {"app/plugins/a.py:f": 16}, "PLR1702": {}}),
    complexity.baseline_payload({"C901": {"app/a.py:f": 15}, "PLR1702": {}}),
    complexity.baseline_payload({"C901": {}}),
])
def test_baseline_rejects_legacy_malformed_or_changed_policy(tmp_path: Path, payload: dict) -> None:
    """格式、范围和阈值异常必须失败，不能静默接受旧口径。"""
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        complexity.read_baseline(path)


@pytest.mark.parametrize(("status", "stdout", "stderr"), [
    (2, "", "tool failure"), (0, "", ""), (0, "{}", ""),
    (1, "[]", ""), (0, '[{"code": "C901"}]', ""), (0, "[]", "warning"),
])
def test_ruff_failures_never_become_zero_debt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, stdout: str, stderr: str,
) -> None:
    """工具异常、输出丢失和退出状态不一致均必须阻断。"""
    result = subprocess.CompletedProcess([], status, stdout, stderr)
    monkeypatch.setattr(complexity.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(ValueError):
        complexity.run_ruff(tmp_path)


@pytest.mark.parametrize("changes", [
    {"code": "invalid-syntax"}, {"message": "unrecognized metric"},
    {"message": "metric (16 > 10)"}, {"location": {"row": 999}},
])
def test_unrecognized_diagnostics_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict,
) -> None:
    """Ruff 升级改变输出或无法定位 owner 时不能漏记债务。"""
    path = _write_source(tmp_path, "app/sample.py", _branches(15))
    diagnostic = {
        "code": "C901", "message": "metric (16 > 15)",
        "filename": str(path), "location": {"row": 1}, **changes,
    }
    monkeypatch.setattr(complexity, "run_ruff", lambda root: [diagnostic])
    with pytest.raises(ValueError):
        complexity.collect_complexity(tmp_path)


def test_missing_or_invalid_source_cannot_shrink_baseline(tmp_path: Path) -> None:
    """扫描根消失或源码损坏不能被当成复杂度下降。"""
    with pytest.raises(ValueError, match="未找到"):
        complexity.collect_complexity(tmp_path)
    _write_source(tmp_path, "app/sample.py", "invalid syntax !")
    with pytest.raises(SyntaxError):
        complexity.collect_complexity(tmp_path)
