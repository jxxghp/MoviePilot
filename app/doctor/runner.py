from __future__ import annotations

import os
import platform
import sys
from datetime import datetime
from typing import Any, Optional

from app.core.config import settings
from app.doctor.checks import default_checks
from app.doctor.models import (
    DoctorFinding,
    DoctorFindingStatus,
    DoctorReport,
    DoctorSeverity,
)
from app.utils.system import SystemUtils
from version import APP_VERSION


class DoctorRunner:
    """
    MoviePilot 离线诊断运行器，负责组合检查项并生成报告。
    """

    def __init__(self, *, fix: bool = False, deep: bool = False):
        """
        初始化诊断运行器。

        :param fix: 是否执行白名单安全修复
        :param deep: 是否执行可能较慢的深度检查
        """
        self.fix = fix
        self.deep = deep
        self.report = DoctorReport(
            generated_at=datetime.now(),
            version=APP_VERSION,
            environment=self._environment(),
        )

    def run(self) -> DoctorReport:
        """
        执行所有默认诊断检查并返回报告。
        """
        for check in default_checks():
            try:
                check(self)
            except Exception as err:
                self.add(
                    finding_id=f"doctor.check_failed.{check.__name__.lstrip('_')}",
                    severity=DoctorSeverity.Error,
                    status=DoctorFindingStatus.Failed,
                    title="诊断检查自身执行失败",
                    detail=f"{check.__name__}: {str(err)}",
                    recommendation="请把该 doctor 报告附加到反馈 Issue，便于修复诊断器本身。",
                )
        return self.report

    def add(
        self,
        *,
        finding_id: str,
        severity: DoctorSeverity,
        status: DoctorFindingStatus,
        title: str,
        detail: str,
        recommendation: str,
        fixable: bool = False,
        fixed: bool = False,
        affects_report_status: bool = True,
        context: Optional[dict[str, Any]] = None,
    ) -> DoctorFinding:
        """
        添加诊断发现并返回该对象。

        :param finding_id: 诊断项稳定标识
        :param severity: 诊断严重级别
        :param status: 单项诊断状态
        :param title: 诊断项标题
        :param detail: 诊断详情
        :param recommendation: 处理建议
        :param fixable: 是否支持 Doctor 自动修复
        :param fixed: 本次运行是否已修复
        :param affects_report_status: 是否参与整体报告状态聚合
        :param context: 可选结构化上下文
        :return: 新增的诊断发现
        """
        finding = DoctorFinding(
            id=finding_id,
            severity=severity,
            status=status,
            title=title,
            detail=detail,
            recommendation=recommendation,
            fixable=fixable,
            fixed=fixed,
            affects_report_status=affects_report_status,
            context=context or {},
        )
        self.report.add_finding(finding)
        return finding

    @staticmethod
    def _environment() -> dict[str, Any]:
        """收集 Doctor 报告所需的本地运行环境信息。"""
        return {
            "runtime": "Docker" if SystemUtils.is_docker() else platform.system(),
            "platform": platform.platform(),
            "python": sys.executable,
            "python_version": platform.python_version(),
            "root_path": str(settings.ROOT_PATH),
            "config_path": str(settings.CONFIG_PATH),
            "log_path": str(settings.LOG_PATH),
            "temp_path": str(settings.TEMP_PATH),
            "is_docker": SystemUtils.is_docker(),
            "safe_mode": settings.MOVIEPILOT_SAFE_MODE,
            "pid": os.getpid(),
        }
