from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Optional


class DoctorSeverity(StrEnum):
    """
    诊断结果严重级别。
    """

    Info = "info"
    Warn = "warn"
    Error = "error"


class DoctorFindingStatus(StrEnum):
    """
    单项诊断状态。
    """

    Ok = "ok"
    Skipped = "skipped"
    Degraded = "degraded"
    Failed = "failed"
    Fixed = "fixed"


class DoctorReportStatus(StrEnum):
    """
    整体诊断报告状态。
    """

    Healthy = "healthy"
    Degraded = "degraded"
    Failed = "failed"


@dataclass
class DoctorFinding:
    """
    单条诊断发现，描述问题、原因、建议和可选修复状态。
    """

    id: str
    severity: DoctorSeverity
    status: DoctorFindingStatus
    title: str
    detail: str
    recommendation: str
    fixable: bool = False
    fixed: bool = False
    affects_report_status: bool = True
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        转换为稳定的 JSON 字典结构。
        """
        payload: dict[str, Any] = {
            "id": self.id,
            "severity": self.severity.value,
            "status": self.status.value,
            "title": self.title,
            "detail": self.detail,
            "recommendation": self.recommendation,
            "fixable": self.fixable,
            "fixed": self.fixed,
            "affects_report_status": self.affects_report_status,
        }
        if self.context:
            payload["context"] = self.context
        return payload


@dataclass
class DoctorReport:
    """
    MoviePilot 离线诊断报告。
    """

    generated_at: datetime
    version: str
    environment: dict[str, Any]
    findings: list[DoctorFinding] = field(default_factory=list)
    schema_version: int = 1

    @property
    def status(self) -> DoctorReportStatus:
        """
        根据诊断发现计算整体状态。
        """
        unresolved = [
            finding
            for finding in self.findings
            if not finding.fixed and finding.affects_report_status
        ]
        if any(finding.severity == DoctorSeverity.Error for finding in unresolved):
            return DoctorReportStatus.Failed
        if any(finding.severity == DoctorSeverity.Warn for finding in unresolved):
            return DoctorReportStatus.Degraded
        return DoctorReportStatus.Healthy

    @property
    def summary(self) -> dict[str, int]:
        """
        统计不同严重级别的诊断发现数量。
        """
        counts = {
            "total": len(self.findings),
            "info": 0,
            "warn": 0,
            "error": 0,
            "fixed": 0,
        }
        for finding in self.findings:
            counts[finding.severity.value] += 1
            if finding.fixed:
                counts["fixed"] += 1
        return counts

    def exit_code(self) -> int:
        """
        返回适合 CLI 和自动化脚本使用的退出码。
        """
        return 2 if self.status == DoctorReportStatus.Failed else 0

    def add_finding(self, finding: DoctorFinding) -> None:
        """
        添加一条诊断发现。
        """
        self.findings.append(finding)

    def find(self, finding_id: str) -> Optional[DoctorFinding]:
        """
        按诊断项 ID 查找发现。
        """
        for finding in self.findings:
            if finding.id == finding_id:
                return finding
        return None

    def to_dict(self) -> dict[str, Any]:
        """
        转换为稳定的 JSON 字典结构。
        """
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "version": self.version,
            "environment": self.environment,
            "summary": self.summary,
            "findings": [finding.to_dict() for finding in self.findings],
        }
