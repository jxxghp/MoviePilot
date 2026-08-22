from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3
from types import SimpleNamespace

from app.runtime.config import settings
from app.doctor import checks
from app.doctor.formatters import format_json_report, format_text_report
from app.doctor.models import DoctorFinding, DoctorFindingStatus, DoctorSeverity
from app.doctor.runner import DoctorRunner, run_doctor


def _current_log_timestamp() -> str:
    """返回 Doctor 近期日志测试使用的当前时间戳。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def test_doctor_reports_valid_backup_when_sqlite_database_is_corrupt(
    tmp_path,
    monkeypatch,
):
    """主数据库损坏时 Doctor 仍应离线校验备份并给出还原命令。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    (tmp_path / "user.db").write_bytes(b"not a sqlite database")
    backup_dir = settings.DATABASE_BACKUP_PATH
    backup_dir.mkdir(parents=True)
    backup = backup_dir / "sqlite_20260822_030000.db"
    with sqlite3.connect(backup) as connection:
        connection.execute("CREATE TABLE entries (value TEXT NOT NULL)")
    (backup_dir / "sqlite_20260822_040000.db").write_bytes(b"invalid newer backup")

    runner = DoctorRunner()
    checks._check_database(runner)

    assert runner.report.find("database.sqlite_open_failed") is not None
    finding = runner.report.find("database.backup_recovery")
    assert finding is not None
    assert finding.status == DoctorFindingStatus.Ok
    assert finding.context["backups"][0]["valid"] is False
    assert finding.context["backups"][1]["valid"] is True
    assert finding.context["restore_command"] == (
        "moviepilot database restore sqlite_20260822_030000.db --confirm"
    )
    assert finding.context["restore_command"] in finding.recommendation


def test_doctor_distinguishes_missing_and_mismatched_backups(tmp_path, monkeypatch):
    """无备份与仅存在其他数据库类型备份应生成不同诊断结论。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    runner = DoctorRunner()
    checks._check_database_backups(runner)
    missing = runner.report.find("database.backup_recovery")
    assert missing is not None
    assert missing.status == DoctorFindingStatus.Skipped

    backup_dir = settings.DATABASE_BACKUP_PATH
    backup_dir.mkdir(parents=True)
    (backup_dir / "postgresql_20260822_030000.dump").write_bytes(b"PGDMP")
    runner = DoctorRunner()
    checks._check_database_backups(runner)
    mismatched = runner.report.find("database.backup_recovery")
    assert mismatched is not None
    assert mismatched.status == DoctorFindingStatus.Degraded
    assert mismatched.context["mismatched"] == ["postgresql_20260822_030000.dump"]


def test_doctor_reports_invalid_backup_without_modifying_it(tmp_path, monkeypatch):
    """Doctor --fix 也只校验备份，不覆盖或删除无效文件。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    backup_dir = settings.DATABASE_BACKUP_PATH
    backup_dir.mkdir(parents=True)
    backup = backup_dir / "sqlite_20260822_030000.db"
    original = b"invalid sqlite backup"
    backup.write_bytes(original)

    runner = DoctorRunner(fix=True)
    checks._check_database_backups(runner)

    finding = runner.report.find("database.backup_recovery")
    assert finding is not None
    assert finding.status == DoctorFindingStatus.Failed
    assert finding.affects_report_status is False
    assert finding.context["backups"][0]["valid"] is False
    assert backup.read_bytes() == original
    assert runner.report.status.value == "healthy"


def test_doctor_exposes_missing_pg_restore_in_text_finding(tmp_path, monkeypatch):
    """PostgreSQL 离线校验工具缺失时应直接告诉用户如何补齐。"""
    def missing_pg_restore(*_args, **_kwargs):
        raise RuntimeError("未找到 pg_restore，请安装 PostgreSQL client 并加入 PATH")

    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DB_TYPE", "postgresql")
    backup_dir = settings.DATABASE_BACKUP_PATH
    backup_dir.mkdir(parents=True)
    (backup_dir / "postgresql_20260822_030000.dump").write_bytes(b"PGDMP")
    monkeypatch.setattr(checks, "verify_database_backup", missing_pg_restore)

    runner = DoctorRunner()
    checks._check_database_backups(runner)

    finding = runner.report.find("database.backup_recovery")
    assert finding is not None
    assert finding.status == DoctorFindingStatus.Failed
    assert "未找到 pg_restore" in finding.detail
    assert "PostgreSQL client" in format_text_report(runner.report)


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
        f"【ERROR】{_current_log_timestamp()} - demo.py - 插件任务执行异常\n",
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
        f"【ERROR】{_current_log_timestamp()} - plugin.py - 加载插件 Demo 出错：boom - Traceback (most recent call last):\n"
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
        f"【INFO】{_current_log_timestamp()} - demo.py - 插件已启动\n",
        encoding="utf-8",
    )
    stdio_log = settings.LOG_PATH / "moviepilot.stdout.log"
    stdio_log.write_text(
        f"ERROR:   [demoplugin] {_current_log_timestamp()} demo.py - task exception\n",
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
        f"【ERROR】{_current_log_timestamp()} - rss.py - 解析 RSS 失败 - Traceback (most recent call last):\n"
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
        f"【ERROR】{_current_log_timestamp()} - plugin.py - 加载插件 Demo 出错：boom - Traceback (most recent call last):\n"
        "Exception: plugin boom\n"
        f"【ERROR】{_current_log_timestamp()} - rss.py - 解析 RSS 失败 - Traceback (most recent call last):\n"
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


def test_doctor_deduplicates_mirrored_plugin_errors(tmp_path, monkeypatch):
    """同一插件错误出现在主日志和插件日志时应只生成一条聚合告警。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    app_log = settings.LOG_PATH / "moviepilot.log"
    plugin_log = settings.LOG_PATH / "plugins" / "demo.log"
    plugin_log.parent.mkdir(parents=True, exist_ok=True)
    app_log.write_text(
        f"【ERROR】{timestamp} - plugin.py - 插件任务执行异常\n",
        encoding="utf-8",
    )
    plugin_log.write_text(
        f"【ERROR】{timestamp} - demo.py - 插件任务执行异常\n",
        encoding="utf-8",
    )

    runner = DoctorRunner()
    checks._check_logs(runner)

    plugin_findings = [
        finding
        for finding in runner.report.findings
        if finding.title == "最近日志存在插件异常"
    ]
    assert len(plugin_findings) == 1
    finding = plugin_findings[0]
    assert finding.context["matches"] == 2
    assert finding.context["unique_matches"] == 1
    assert len(finding.context["log_files"]) == 2
    assert runner.report.summary["advisory"] == 1
    assert runner.report.find("logs.recent") is not None


def test_doctor_new_layout_plugin_log_error_does_not_degrade_report(tmp_path, monkeypatch):
    """新版按实例分目录的插件日志中的错误也应保留告警，但不降低系统整体状态。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    plugin_log = settings.PLUGIN_DATA_PATH / "DemoPlugin" / "second" / "logs" / "plugin.log"
    plugin_log.parent.mkdir(parents=True, exist_ok=True)
    plugin_log.write_text(
        f"【ERROR】{_current_log_timestamp()} - demo.py - 插件任务执行异常\n",
        encoding="utf-8",
    )

    runner = DoctorRunner()
    checks._check_logs(runner)

    finding = runner.report.find("logs.plugin.recent_errors")
    assert finding is not None
    assert finding.status == DoctorFindingStatus.Degraded
    assert finding.severity == DoctorSeverity.Warn
    assert finding.affects_report_status is False
    assert finding.context["component"] == "plugin"
    assert runner.report.status.value == "healthy"


def test_doctor_scans_both_legacy_and_new_layout_plugin_logs(tmp_path, monkeypatch):
    """同一次巡检应同时扫到旧版扁平布局和新版实例目录布局的插件日志。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    timestamp = _current_log_timestamp()
    legacy_log = settings.LOG_PATH / "plugins" / "legacy.log"
    legacy_log.parent.mkdir(parents=True, exist_ok=True)
    legacy_log.write_text(
        f"【ERROR】{timestamp} - demo.py - 旧版插件任务异常\n", encoding="utf-8"
    )
    new_log = settings.PLUGIN_DATA_PATH / "NewPlugin" / "default" / "logs" / "plugin.log"
    new_log.parent.mkdir(parents=True, exist_ok=True)
    new_log.write_text(
        f"【ERROR】{timestamp} - demo.py - 新版插件任务异常\n", encoding="utf-8"
    )

    runner = DoctorRunner()
    checks._check_logs(runner)

    # 插件组件按 component 聚合成一条告警，不按文件拆分；断言两个布局的错误行
    # 都进了这条告警的详情和来源文件列表，确认新位置没有被漏扫
    plugin_findings = [
        finding
        for finding in runner.report.findings
        if finding.title == "最近日志存在插件异常"
    ]
    assert len(plugin_findings) == 1
    finding = plugin_findings[0]
    assert "旧版插件任务异常" in finding.detail
    assert "新版插件任务异常" in finding.detail
    assert str(settings.LOG_PATH / "plugins" / "legacy.log") in finding.context["log_files"]
    assert str(new_log) in finding.context["log_files"]
    assert runner.report.status.value == "healthy"


def test_doctor_ignores_errors_outside_log_window(tmp_path, monkeypatch):
    """超出日志诊断时间窗的历史错误不应污染当前 Doctor 结果。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    timestamp = (datetime.now() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    app_log = settings.LOG_PATH / "moviepilot.log"
    app_log.parent.mkdir(parents=True, exist_ok=True)
    app_log.write_text(
        f"【ERROR】{timestamp} - rss.py - 历史解析错误\n",
        encoding="utf-8",
    )

    runner = DoctorRunner()
    checks._check_logs(runner)

    assert runner.report.find("logs.moviepilot.recent_errors") is None
    assert runner.report.find("logs.recent") is not None
    assert runner.report.status.value == "healthy"
