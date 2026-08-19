"""Doctor 对插件多版本目录布局静态扫描结论的呈现合同测试。"""

from __future__ import annotations

from pathlib import Path

from app.doctor import checks
from app.doctor.formatters import format_json_report, format_text_report
from app.doctor.models import DoctorFindingStatus, DoctorSeverity
from app.doctor.runner import DoctorRunner


def _write_plugin(root: Path, plugin_id: str, source: str) -> Path:
    """写入一个仅用于 doctor 检查的最小插件源码目录。"""
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(source, encoding="utf-8")
    return plugin_dir


def test_no_plugin_directories_reports_skipped_finding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """没有已安装插件源码目录时应给出跳过态发现，不影响整体状态。"""
    plugins_dir = tmp_path / "plugins"
    monkeypatch.setattr(checks, "_plugins_root_dir", lambda: plugins_dir)

    runner = DoctorRunner()
    checks._check_plugin_version_layout(runner)

    finding = runner.report.find("plugins.version_layout.none")
    assert finding is not None
    assert finding.status == DoctorFindingStatus.Skipped
    assert finding.affects_report_status is False


def test_clean_plugin_reports_ok_with_all_three_criteria_false(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """只用相对 import、不依赖其它插件、不继承共享 Base 的插件应报告 Ok。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "cleanplugin", "from .utils import helper\n")
    (plugins_dir / "cleanplugin" / "utils.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(checks, "_plugins_root_dir", lambda: plugins_dir)

    runner = DoctorRunner()
    checks._check_plugin_version_layout(runner)

    finding = runner.report.find("plugins.version_layout.cleanplugin")
    assert finding is not None
    assert finding.status == DoctorFindingStatus.Ok
    assert finding.severity == DoctorSeverity.Info
    assert finding.affects_report_status is False
    assert finding.context["has_self_referential_imports"] is False
    assert finding.context["has_cross_plugin_imports"] is False
    assert finding.context["has_shared_base_models"] is False


def test_plugin_with_all_three_issues_reports_actionable_detail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """自引用绝对 import、跨插件依赖、共享基类建模应同时在一条发现中报出。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(
        plugins_dir,
        "riskyplugin",
        (
            "from app.plugins.riskyplugin.utils import helper\n"
            "from app.plugins.otherplugin import Thing\n"
            "from app.db import Base\n\n"
            "class RiskyData(Base):\n"
            "    pass\n"
        ),
    )
    (plugins_dir / "riskyplugin" / "utils.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(checks, "_plugins_root_dir", lambda: plugins_dir)

    runner = DoctorRunner()
    checks._check_plugin_version_layout(runner)

    finding = runner.report.find("plugins.version_layout.riskyplugin")
    assert finding is not None
    assert finding.status == DoctorFindingStatus.Degraded
    assert finding.severity == DoctorSeverity.Warn
    assert finding.affects_report_status is False
    assert finding.context["has_self_referential_imports"] is True
    assert finding.context["has_cross_plugin_imports"] is True
    assert finding.context["has_shared_base_models"] is True
    assert "__init__.py:1" in finding.detail
    assert "from .utils import helper" in finding.detail
    assert "otherplugin" in finding.detail
    assert "class RiskyData" in finding.detail
    assert runner.report.status.value == "healthy"


def test_multiple_plugins_each_get_their_own_finding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """每个已安装插件都应各自拥有一条独立的诊断发现。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "pluginone", "value = 1\n")
    _write_plugin(plugins_dir, "plugintwo", "from app.plugins.plugintwo.core import run\n")
    (plugins_dir / "plugintwo" / "core.py").write_text("def run():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(checks, "_plugins_root_dir", lambda: plugins_dir)

    runner = DoctorRunner()
    checks._check_plugin_version_layout(runner)

    finding_one = runner.report.find("plugins.version_layout.pluginone")
    finding_two = runner.report.find("plugins.version_layout.plugintwo")
    assert finding_one is not None and finding_one.status == DoctorFindingStatus.Ok
    assert finding_two is not None and finding_two.status == DoctorFindingStatus.Degraded


def test_syntax_error_plugin_file_does_not_crash_doctor_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """插件源码语法错误不得让 doctor 整体检查崩溃，应记录为无法解析。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "brokenplugin", "def broken(:\n    pass\n")
    monkeypatch.setattr(checks, "_plugins_root_dir", lambda: plugins_dir)

    runner = DoctorRunner()
    checks._check_plugin_version_layout(runner)

    finding = runner.report.find("plugins.version_layout.brokenplugin")
    assert finding is not None
    assert finding.status == DoctorFindingStatus.Degraded
    assert "__init__.py" in finding.detail
    assert "无法解析" in finding.detail


def test_doctor_report_surfaces_plugin_version_layout_conclusion_in_text_and_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """doctor 完整报告（文本与 JSON）都应包含插件多版本布局扫描结论。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(
        plugins_dir,
        "riskyplugin",
        "from app.plugins.riskyplugin.utils import helper\n",
    )
    (plugins_dir / "riskyplugin" / "utils.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(checks, "_plugins_root_dir", lambda: plugins_dir)

    runner = DoctorRunner()
    checks._check_plugin_version_layout(runner)

    text = format_text_report(runner.report)
    json_text = format_json_report(runner.report)

    assert "plugins.version_layout.riskyplugin" in text
    assert "from .utils import helper" in text
    assert '"plugins.version_layout.riskyplugin"' in json_text
    assert "has_self_referential_imports" in json_text
