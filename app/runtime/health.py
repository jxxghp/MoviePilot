"""
进程内应用健康状态。
"""

from dataclasses import dataclass
from enum import StrEnum

from fastapi import FastAPI


class ReadinessPhase(StrEnum):
    """
    应用生命周期对公开 readiness 探针暴露的最小阶段。
    """

    # 应用正在启动，尚不能接受正常业务流量
    STARTING = "starting"
    # 数据库和全部必需组件均已就绪
    READY = "ready"
    # 应用正在关闭，撤销就绪状态
    STOPPING = "stopping"
    # 启动存在不可恢复的错误
    FAILED = "failed"


@dataclass(slots=True)
class ApplicationHealth:
    """
    保存单个 FastAPI 实例的数据库和生命周期就绪状态。

    :param phase: 当前生命周期阶段，初始为启动中
    :param database_ready: 数据库迁移和版本检查是否完成，初始为 False
    """

    phase: ReadinessPhase = ReadinessPhase.STARTING
    database_ready: bool = False

    @property
    def is_ready(self) -> bool:
        """
        判断数据库和完整生命周期是否均已就绪。

        :return: 数据库检查已完成且生命周期为 READY 时返回 True
        """
        return self.phase is ReadinessPhase.READY and self.database_ready

    def begin_startup(self) -> None:
        """
        重置一次 lifespan 启动尝试的状态。
        """
        self.phase = ReadinessPhase.STARTING
        self.database_ready = False

    def mark_database_ready(self) -> None:
        """
        记录数据库迁移和 head 校验已经完成。
        """
        self.database_ready = True

    def mark_ready(self) -> None:
        """
        记录所有 fail-fast 生命周期组件已经启动。
        """
        if not self.database_ready:
            raise RuntimeError("数据库尚未就绪，不能发布应用 ready 状态")
        self.phase = ReadinessPhase.READY

    def mark_failed(self) -> None:
        """
        记录启动存在不可恢复失败。
        """
        self.phase = ReadinessPhase.FAILED

    def mark_stopping(self) -> None:
        """
        在资源关闭前撤销 readiness。
        """
        self.phase = ReadinessPhase.STOPPING


def get_application_health(app: FastAPI) -> ApplicationHealth:
    """
    读取应用级健康状态；为直接构造的测试应用补齐默认状态。
    """
    health = getattr(app.state, "moviepilot_health", None)
    if health is None:
        health = ApplicationHealth()
        app.state.moviepilot_health = health
    return health
