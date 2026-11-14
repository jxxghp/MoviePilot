"""
离线诊断的发现项、汇总状态及报告序列化结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Optional


class DoctorSeverity(StrEnum):
    """
    诊断结果严重级别。
    """

    # 检查通过或提供参考信息，不单独使报告降级
    Info = "info"
    # 存在未解决的风险，参与报告状态时使其降级
    Warn = "warn"
    # 存在阻止正常运行的问题，参与报告状态时使其失败
    Error = "error"


class DoctorFindingStatus(StrEnum):
    """
    单项诊断状态。
    """

    # 检查已完成且未发现问题
    Ok = "ok"
    # 当前环境或检查模式不执行该项
    Skipped = "skipped"
    # 检查发现风险或能力受限
    Degraded = "degraded"
    # 检查发现故障或检查本身无法完成
    Failed = "failed"
    # 本次检查已完成允许的自动修复
    Fixed = "fixed"


class DoctorReportStatus(StrEnum):
    """
    整体诊断报告状态。
    """

    # 没有参与聚合的未修复警告或错误
    Healthy = "healthy"
    # 检查发现风险或能力受限
    Degraded = "degraded"
    # 检查发现故障或检查本身无法完成
    Failed = "failed"


@dataclass
class DoctorFinding:
    """
    单条诊断发现，描述问题、原因、建议和可选修复状态。

    :param id: 诊断项的稳定标识，用于在报告中查找对应发现
    :param severity: 发现的严重级别，影响未修复问题的报告状态
    :param status: 本次检查的执行结果，包括跳过、失败和已修复
    :param title: 面向用户展示的简短标题
    :param detail: 问题依据或检查结果，不应包含凭据原文
    :param recommendation: 建议用户执行的处理步骤
    :param fixable: 是否支持自动修复，默认为 False
    :param fixed: 本次检查是否已完成修复，默认为 False
    :param affects_report_status: 是否参与报告状态计算，默认为 True；False 表示仅提供参考信息
    :param context: 附加的结构化诊断信息，每个发现独立创建空字典
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
        转换为可序列化的诊断字段字典。

        :return: 枚举转换为字符串后的字段；context 为空时省略该键
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

    :param generated_at: 生成报告时的时间，序列化时保留到秒
    :param version: 本次诊断对应的 MoviePilot 版本
    :param environment: 运行平台、配置路径和进程等环境信息
    :param findings: 按检查顺序收集的发现项，每份报告独立创建空列表
    :param schema_version: 报告结构版本，默认为 1，供消费方识别字段协议
    """

    generated_at: datetime
    version: str
    environment: dict[str, Any]
    findings: list[DoctorFinding] = field(default_factory=list)
    schema_version: int = 1

    @property
    def status(self) -> DoctorReportStatus:
        """
        根据尚未修复且参与聚合的发现计算报告状态。

        :return: 存在错误时为失败，否则有警告时为降级，其余情况为健康
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
        统计诊断发现的总数、严重级别和处理状态。

        :return: 总数及各严重级别、已修复、仅供参考项的数量；严重级别计数包含已修复项
        """
        counts = {
            "total": len(self.findings),
            "info": 0,
            "warn": 0,
            "error": 0,
            "fixed": 0,
            "advisory": 0,
        }
        for finding in self.findings:
            counts[finding.severity.value] += 1
            if finding.fixed:
                counts["fixed"] += 1
            elif not finding.affects_report_status:
                counts["advisory"] += 1
        return counts

    def exit_code(self) -> int:
        """
        返回供命令行和自动化脚本使用的退出码。

        :return: 报告失败时返回 2；健康或仅降级时返回 0
        """
        return 2 if self.status == DoctorReportStatus.Failed else 0

    def add_finding(self, finding: DoctorFinding) -> None:
        """
        按检查顺序追加诊断发现。

        :param finding: 要加入报告的发现对象；不去重，也不复制对象
        """
        self.findings.append(finding)

    def find(self, finding_id: str) -> Optional[DoctorFinding]:
        """
        按稳定标识查找报告中的诊断发现。

        :param finding_id: 诊断项的稳定标识
        :return: 第一条匹配的发现；没有匹配项时返回 None
        """
        for finding in self.findings:
            if finding.id == finding_id:
                return finding
        return None

    def to_dict(self) -> dict[str, Any]:
        """
        生成包含环境、状态、汇总和发现项的报告字典。

        :return: 时间转换为秒级 ISO 格式、枚举转换为字符串的报告结构
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
