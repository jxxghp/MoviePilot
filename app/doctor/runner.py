"""
组织离线检查，收集运行环境并生成可供命令行使用的诊断报告。
"""

from __future__ import annotations

import os
import platform
import sys
from datetime import datetime
from typing import Any, Optional, cast

from app.adapters.system.host import SystemUtils
from app.doctor.checks import DoctorRunnerProtocol, default_checks
from app.doctor.models import (
    DoctorFinding,
    DoctorFindingStatus,
    DoctorReport,
    DoctorSeverity,
)
from app.runtime.settings import get_runtime_setting
from app.runtime.version import get_app_version


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
            version=get_app_version(),
            environment=self._environment(),
        )

    def run(self) -> DoctorReport:
        """
        依次执行默认检查，将检查自身抛出的异常记录为错误发现。

        :return: 当前运行器持有的报告；重复运行会继续向同一报告追加发现
        """
        for check in default_checks():
            try:
                check(cast(DoctorRunnerProtocol, self))
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
        """
        收集 Doctor 报告所需的本地运行环境信息。
        """
        return {
            "runtime": "Docker" if SystemUtils.is_docker() else platform.system(),
            "platform": platform.platform(),
            "python": sys.executable,
            "python_version": platform.python_version(),
            "root_path": str(get_runtime_setting('ROOT_PATH')),
            "config_path": str(get_runtime_setting('CONFIG_PATH')),
            "log_path": str(get_runtime_setting('LOG_PATH')),
            "temp_path": str(get_runtime_setting('TEMP_PATH')),
            "is_docker": SystemUtils.is_docker(),
            "safe_mode": get_runtime_setting('MOVIEPILOT_SAFE_MODE'),
            "pid": os.getpid(),
        }


def run_doctor(*, fix: bool = False, deep: bool = False) -> DoctorReport:
    """
    创建运行器并执行一次 MoviePilot 离线诊断。

    :param fix: 是否执行检查项允许的安全修复，默认只读检查
    :param deep: 是否执行可能较慢或需要连接配置服务的深度检查
    :return: 包含本次环境快照和检查发现的报告
    """
    return DoctorRunner(fix=fix, deep=deep).run()
