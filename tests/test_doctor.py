from __future__ import annotations

from types import SimpleNamespace

from app.core.config import settings
from app.doctor import checks, run_doctor
from app.doctor.formatters import format_json_report, format_text_report
from app.doctor.models import DoctorFinding, DoctorFindingStatus, DoctorSeverity
from app.doctor.runner import DoctorRunner


def test_doctor_report_has_stable_json_shape(tmp_path, monkeypatch):
    """doctor JSON 报告应包含稳定状态、环境、汇总和发现列表。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    (settings.LOG_PATH).mkdir(parents=True, exist_ok=True)
    (settings.ROOT_PATH / "public").mkdir(exist_ok=True)

    report = run_doctor()
    payload = report.to_dict()

    assert payload["schema_version"] == 1
    assert payload["status"] in {"healthy", "degraded", "failed"}
    assert payload["environment"]["config_path"] == str(tmp_path)
    assert isinstance(payload["summary"]["total"], int)
    assert isinstance(payload["findings"], list)
    assert all("affects_report_status" in item for item in payload["findings"])
    assert any(item["id"] == "runtime.paths" for item in payload["findings"])


def test_doctor_formatters_include_status_and_finding(tmp_path, monkeypatch):
    """doctor 文本和 JSON 格式化应展示状态与诊断项。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    report = run_doctor()
    report.add_finding(
        DoctorFinding(
            id="test.demo",
            severity=DoctorSeverity.Warn,
            status=DoctorFindingStatus.Degraded,
            title="测试诊断项",
            detail="测试原因",
            recommendation="测试建议",
        )
    )

    text = format_text_report(report)
    json_text = format_json_report(report)

    assert "MoviePilot Doctor" in text
    assert "测试诊断项" in text
    assert '"schema_version": 1' in json_text
    assert '"test.demo"' in json_text


def test_doctor_fix_removes_stale_runtime(tmp_path, monkeypatch):
    """doctor --fix 应清理指向失效进程的 runtime 文件。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    settings.TEMP_PATH.mkdir(parents=True, exist_ok=True)
    runtime_file = settings.TEMP_PATH / "moviepilot.runtime.json"
    runtime_file.write_text('{"pid": 999999, "create_time": 1}', encoding="utf-8")

    report = run_doctor(fix=True)

    assert not runtime_file.exists()
    finding = report.find("runtime.backend_stale")
    assert finding is not None
    assert finding.fixed


def test_doctor_accepts_healthy_unmanaged_backend_port(monkeypatch):
    """doctor 在容器中应把健康的非 CLI 管理后端端口识别为正常。"""
    occupant = SimpleNamespace(pid=12345)
    monkeypatch.setattr(checks, "_port_occupants", lambda port: [occupant])
    monkeypatch.setattr(checks, "_process_description", lambda process: f"PID {process.pid} (python)")
    monkeypatch.setattr(checks, "_is_expected_port_process", lambda name, process: False)
    monkeypatch.setattr(
        checks,
        "_backend_health_payload",
        lambda port: {"success": True, "data": {"BACKEND_VERSION": "v2-test"}},
    )

    runner = DoctorRunner()
    checks._check_port(runner, name="backend", port=3001, managed_process=None)

    finding = runner.report.find("port.backend_listening_unmanaged")
    assert finding is not None
    assert finding.status == DoctorFindingStatus.Ok
    assert finding.severity == DoctorSeverity.Info
    assert finding.context["backend_version"] == "v2-test"
    assert runner.report.find("port.backend_occupied") is None


def test_doctor_plugin_log_error_does_not_degrade_report(tmp_path, monkeypatch):
    """插件独立日志中的错误应保留告警，但不降低系统整体状态。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    plugin_log = settings.LOG_PATH / "plugins" / "demo.log"
    plugin_log.parent.mkdir(parents=True, exist_ok=True)
    plugin_log.write_text(
        "【ERROR】2026-07-20 08:00:00 - demo.py - 插件任务执行异常\n",
        encoding="utf-8",
    )

    runner = DoctorRunner()
    checks._check_logs(runner)

    finding = runner.report.find("logs.demo.recent_errors")
    assert finding is not None
    assert finding.status == DoctorFindingStatus.Degraded
    assert finding.severity == DoctorSeverity.Warn
    assert finding.affects_report_status is False
    assert finding.context["component"] == "plugin"
    assert runner.report.status.value == "healthy"
    assert "[WARN/ADVISORY]" in format_text_report(runner.report)


def test_doctor_plugin_load_error_in_main_log_does_not_degrade_report(
    tmp_path,
    monkeypatch,
):
    """主日志中的插件加载异常应归入插件告警，不降低系统整体状态。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    app_log = settings.LOG_PATH / "moviepilot.log"
    app_log.parent.mkdir(parents=True, exist_ok=True)
    app_log.write_text(
        "【ERROR】2026-07-20 08:00:00 - plugin.py - 加载插件 Demo 出错：boom - Traceback (most recent call last):\n"
        "Exception: boom\n",
        encoding="utf-8",
    )

    runner = DoctorRunner()
    checks._check_logs(runner)

    finding = runner.report.find("logs.moviepilot.recent_errors")
    assert finding is not None
    assert finding.affects_report_status is False
    assert finding.context["component"] == "plugin"
    assert "Exception: boom" in finding.detail
    assert runner.report.status.value == "healthy"


def test_doctor_plugin_error_mirrored_to_stdio_does_not_degrade_report(
    tmp_path,
    monkeypatch,
):
    """插件错误镜像到后端 stdio 日志时仍不应降低系统整体状态。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    plugin_log = settings.LOG_PATH / "plugins" / "DemoPlugin.log"
    plugin_log.parent.mkdir(parents=True, exist_ok=True)
    plugin_log.write_text(
        "【INFO】2026-07-20 08:00:00 - demo.py - 插件已启动\n",
        encoding="utf-8",
    )
    stdio_log = settings.LOG_PATH / "moviepilot.stdout.log"
    stdio_log.write_text(
        "ERROR:   [demoplugin] 2026-07-20 08:01:00 demo.py - task exception\n",
        encoding="utf-8",
    )

    runner = DoctorRunner()
    checks._check_logs(runner)

    finding = runner.report.find("logs.moviepilot.stdout.recent_errors")
    assert finding is not None
    assert finding.affects_report_status is False
    assert finding.context["component"] == "plugin"
    assert runner.report.status.value == "healthy"


def test_doctor_core_log_error_still_degrades_report(tmp_path, monkeypatch):
    """核心日志错误仍应参与系统整体状态聚合。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    app_log = settings.LOG_PATH / "moviepilot.log"
    app_log.parent.mkdir(parents=True, exist_ok=True)
    app_log.write_text(
        "【ERROR】2026-07-20 08:00:00 - rss.py - 解析 RSS 失败 - Traceback (most recent call last):\n"
        "RuntimeError: boom\n",
        encoding="utf-8",
    )

    runner = DoctorRunner()
    checks._check_logs(runner)

    finding = runner.report.find("logs.moviepilot.recent_errors")
    assert finding is not None
    assert finding.affects_report_status is True
    assert finding.context["component"] == "core"
    assert runner.report.status.value == "degraded"


def test_doctor_mixed_plugin_and_core_log_errors_keep_core_status(
    tmp_path,
    monkeypatch,
):
    """同一主日志混有插件和核心错误时，仅核心错误应影响整体状态。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    app_log = settings.LOG_PATH / "moviepilot.log"
    app_log.parent.mkdir(parents=True, exist_ok=True)
    app_log.write_text(
        "【ERROR】2026-07-20 08:00:00 - plugin.py - 加载插件 Demo 出错：boom - Traceback (most recent call last):\n"
        "Exception: plugin boom\n"
        "【ERROR】2026-07-20 08:01:00 - rss.py - 解析 RSS 失败 - Traceback (most recent call last):\n"
        "Exception: core boom\n",
        encoding="utf-8",
    )

    runner = DoctorRunner()
    checks._check_logs(runner)

    core_finding = runner.report.find("logs.moviepilot.recent_errors")
    plugin_finding = runner.report.find("logs.moviepilot.plugin_errors")
    assert core_finding is not None
    assert plugin_finding is not None
    assert core_finding.affects_report_status is True
    assert plugin_finding.affects_report_status is False
    assert "core boom" in core_finding.detail
    assert "plugin boom" in plugin_finding.detail
    assert runner.report.status.value == "degraded"
