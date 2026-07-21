from __future__ import annotations

import json

from app.doctor.models import DoctorFinding, DoctorReport


STATUS_LABELS = {
    "healthy": "healthy",
    "degraded": "degraded",
    "failed": "failed",
}


def format_json_report(report: DoctorReport) -> str:
    """
    将诊断报告格式化为 JSON 文本。
    """
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)


def format_text_report(report: DoctorReport) -> str:
    """
    将诊断报告格式化为面向用户阅读的文本。
    """
    lines = [
        "MoviePilot Doctor",
        "",
        f"状态: {STATUS_LABELS.get(report.status.value, report.status.value)}",
        f"版本: {report.version}",
        f"生成时间: {report.generated_at.isoformat(timespec='seconds')}",
        f"运行环境: {report.environment.get('runtime', 'unknown')}",
        f"配置目录: {report.environment.get('config_path', '')}",
        "",
    ]
    for finding in report.findings:
        lines.extend(_format_finding(finding))
    summary = report.summary
    lines.extend([
        "",
        f"汇总: total={summary['total']} error={summary['error']} warn={summary['warn']} fixed={summary['fixed']}",
    ])
    return "\n".join(lines)


def _format_finding(finding: DoctorFinding) -> list[str]:
    marker = finding.severity.value.upper()
    if finding.fixed:
        marker = "FIXED"
    elif not finding.affects_report_status:
        marker = f"{marker}/ADVISORY"
    lines = [f"[{marker}] {finding.title}", f"ID: {finding.id}"]
    if finding.detail:
        lines.append(f"原因: {finding.detail}")
    if finding.recommendation:
        lines.append(f"建议: {finding.recommendation}")
    lines.append("")
    return lines
