"""应用级数据维护用例。

本模块拥有保留期、批次循环、进度和部分失败汇总语义。具体数据库表如何删除由
``CleanupRepository`` 端口提供，调度器只负责触发用例。
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, ContextManager, Dict, Optional, Protocol

from app.runtime.config import settings
from app.runtime.log import logger


CleanupProgress = Callable[..., None]


@dataclass(frozen=True, slots=True)
class CleanupPolicy:
    """描述一次数据维护运行使用的总开关和各表保留期。"""

    enabled: bool
    message_days: int
    download_history_days: int
    site_userdata_days: int
    transfer_history_days: int
    download_failure_days: int


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    """描述单张表的保留期、截止点和批量删除动作。"""

    name: str
    retention_days: int
    cutoff: str
    delete_batch: Callable[[Any], int]


class CleanupRepository(Protocol):
    """数据维护用例需要的最小持久化端口。"""

    def session(self) -> ContextManager[Any]:
        """返回一次维护运行共用的数据库会话上下文。"""
        ...

    def unit_of_work(self, db: Any) -> "CleanupUnitOfWork":
        """返回绑定到当前维护会话的事务边界。"""
        ...

    def delete_messages(self, db: Any, cutoff: str, limit: int) -> int:
        """删除早于截止时间的消息。"""
        ...

    def delete_download_history(self, db: Any, cutoff: str, limit: int) -> int:
        """删除早于截止时间的下载历史。"""
        ...

    def delete_download_orphans(self, db: Any, limit: int) -> int:
        """删除已经失去父下载历史的文件记录。"""
        ...

    def delete_site_userdata(self, db: Any, cutoff: str, limit: int) -> int:
        """删除早于截止日期的站点用户数据快照。"""
        ...

    def delete_transfer_history(self, db: Any, cutoff: str, limit: int) -> int:
        """删除早于截止时间的整理历史。"""
        ...

    def delete_download_failures(self, db: Any, cutoff: str, limit: int) -> int:
        """删除已经过期的下载失败冷却记录。"""
        ...


class CleanupUnitOfWork(Protocol):
    """数据维护每一批删除所需的最小事务能力。"""

    def commit(self) -> None:
        """提交当前批次。"""
        ...

    def rollback(self) -> None:
        """回滚失败批次并恢复会话可用状态。"""
        ...


class DataCleanupService:
    """按配置执行分批数据清理并生成兼容报告。"""

    DEFAULT_BATCH_SIZE = 500

    def __init__(
        self,
        *,
        repository: CleanupRepository,
        policy_reader: Callable[[], CleanupPolicy],
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        """保存持久化端口、动态配置读取器和可测试时钟。"""
        self._repository = repository
        self._policy_reader = policy_reader
        self._clock = clock

    def execute(
        self,
        batch_size: Optional[int] = None,
        progress_callback: Optional[CleanupProgress] = None,
    ) -> Dict[str, Any]:
        """执行全部清理计划，保持旧调度入口的报告和异常语义。"""
        started_at = self._clock()
        normalized_batch_size = batch_size or self.DEFAULT_BATCH_SIZE
        if normalized_batch_size <= 0:
            normalized_batch_size = self.DEFAULT_BATCH_SIZE
        policy = self._policy_reader()
        report: Dict[str, Any] = {
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "batch_size": normalized_batch_size,
            "enabled": policy.enabled,
            "tables": {},
            "total_deleted": 0,
        }
        if not policy.enabled:
            report["skipped_reason"] = "disabled"
            logger.info("数据表清理总开关未开启，跳过执行")
            return report

        plans = self._build_plans(
            policy=policy,
            started_at=started_at,
            batch_size=normalized_batch_size,
        )
        if progress_callback:
            progress_callback(value=0, text="开始清理数据表 ...")

        errors: list[str] = []
        with self._repository.session() as db:
            for plan_index, plan in enumerate(plans):
                self._execute_plan(
                    db=db,
                    plan=plan,
                    plan_index=plan_index,
                    total_plans=len(plans),
                    report=report,
                    errors=errors,
                    progress_callback=progress_callback,
                )

        if errors:
            report["errors"] = errors
            logger.error(
                f"数据表清理部分失败：{json.dumps(report, ensure_ascii=False)}"
            )
            raise RuntimeError("；".join(errors))

        logger.info(f"数据表清理完成：{json.dumps(report, ensure_ascii=False)}")
        return report

    def _execute_plan(
        self,
        *,
        db: Any,
        plan: CleanupPlan,
        plan_index: int,
        total_plans: int,
        report: Dict[str, Any],
        errors: list[str],
        progress_callback: Optional[CleanupProgress],
    ) -> None:
        """执行单表计划并把成功、跳过或失败状态写入总报告。"""
        if plan.retention_days <= 0:
            report["tables"][plan.name] = {
                "deleted": 0,
                "batches": 0,
                "cutoff": None,
                "retention_days": plan.retention_days,
                "skipped": True,
                "reason": "retention_days<=0",
            }
            if progress_callback:
                progress_callback(
                    value=(plan_index + 1) / total_plans * 100,
                    text=f"数据表 {plan.name} 跳过清理",
                )
            return

        try:
            if progress_callback:
                progress_callback(
                    value=plan_index / total_plans * 100,
                    text=f"正在清理数据表 {plan.name} ...",
                )
            unit_of_work = self._repository.unit_of_work(db)
            table_report = self._cleanup_in_batches(
                db=db,
                table_name=plan.name,
                delete_batch=plan.delete_batch,
                unit_of_work=unit_of_work,
            )
            table_report["cutoff"] = plan.cutoff
            table_report["retention_days"] = plan.retention_days
            report["tables"][plan.name] = table_report
            report["total_deleted"] += table_report["deleted"]
        except Exception as err:
            self._repository.unit_of_work(db).rollback()
            errors.append(f"{plan.name}: {str(err)}")
            logger.error(f"数据表 {plan.name} 清理失败：{str(err)}")
            report["tables"][plan.name] = {
                "deleted": 0,
                "batches": 0,
                "cutoff": plan.cutoff,
                "retention_days": plan.retention_days,
                "error": str(err),
            }
        finally:
            if progress_callback:
                progress_callback(
                    value=(plan_index + 1) / total_plans * 100,
                    text=f"数据表 {plan.name} 清理处理完成",
                )

    def _build_plans(
        self,
        *,
        policy: CleanupPolicy,
        started_at: datetime,
        batch_size: int,
    ) -> list[CleanupPlan]:
        """把一次动态配置快照转换为固定顺序的清理计划。"""
        message_cutoff = self._cutoff(started_at, policy.message_days, "%Y-%m-%d")
        download_history_cutoff = self._cutoff(
            started_at,
            policy.download_history_days,
            "%Y-%m-%d",
        )
        site_userdata_cutoff = self._cutoff(
            started_at,
            policy.site_userdata_days,
            "%Y-%m-%d",
        )
        transfer_history_cutoff = self._cutoff(
            started_at,
            policy.transfer_history_days,
            "%Y-%m-%d",
        )
        download_failure_cutoff = self._cutoff(
            started_at,
            policy.download_failure_days,
            "%Y-%m-%d %H:%M:%S",
        )
        return [
            CleanupPlan(
                "message",
                policy.message_days,
                message_cutoff,
                lambda db: self._repository.delete_messages(
                    db, message_cutoff, batch_size
                ),
            ),
            CleanupPlan(
                "downloadhistory",
                policy.download_history_days,
                download_history_cutoff,
                lambda db: self._repository.delete_download_history(
                    db, download_history_cutoff, batch_size
                ),
            ),
            CleanupPlan(
                "downloadfiles",
                policy.download_history_days,
                "follow-parent-history",
                lambda db: self._repository.delete_download_orphans(db, batch_size),
            ),
            CleanupPlan(
                "siteuserdata",
                policy.site_userdata_days,
                site_userdata_cutoff,
                lambda db: self._repository.delete_site_userdata(
                    db, site_userdata_cutoff, batch_size
                ),
            ),
            CleanupPlan(
                "transferhistory",
                policy.transfer_history_days,
                transfer_history_cutoff,
                lambda db: self._repository.delete_transfer_history(
                    db, transfer_history_cutoff, batch_size
                ),
            ),
            CleanupPlan(
                "downloadfailure",
                policy.download_failure_days,
                download_failure_cutoff,
                lambda db: self._repository.delete_download_failures(
                    db, download_failure_cutoff, batch_size
                ),
            ),
        ]

    def _cleanup_in_batches(
        self,
        *,
        db: Any,
        table_name: str,
        delete_batch: Callable[[Any], int],
        unit_of_work: CleanupUnitOfWork,
    ) -> Dict[str, int]:
        """循环执行单表分批删除，直到持久化端口返回零。"""
        total_deleted = 0
        batches = 0
        while True:
            deleted = delete_batch(db) or 0
            if deleted <= 0:
                break
            unit_of_work.commit()
            batches += 1
            total_deleted += deleted
            logger.info(
                f"数据表 {table_name} 清理第 {batches} 批完成，删除 {deleted} 条记录"
            )
        return {"deleted": total_deleted, "batches": batches}

    @staticmethod
    def _cutoff(started_at: datetime, retention_days: int, pattern: str) -> str:
        """按兼容格式计算一个清理截止时间。"""
        return (started_at - timedelta(days=retention_days)).strftime(pattern)


def read_cleanup_policy() -> CleanupPolicy:
    """读取并规范化当前数据清理配置，单次运行期间保持快照一致。"""
    return CleanupPolicy(
        enabled=bool(settings.DATA_CLEANUP_ENABLE),
        message_days=_normalize_days(settings.DATA_CLEANUP_MESSAGE_DAYS),
        download_history_days=_normalize_days(
            settings.DATA_CLEANUP_DOWNLOAD_HISTORY_DAYS
        ),
        site_userdata_days=_normalize_days(settings.DATA_CLEANUP_SITE_USERDATA_DAYS),
        transfer_history_days=_normalize_days(
            settings.DATA_CLEANUP_TRANSFER_HISTORY_DAYS
        ),
        download_failure_days=_normalize_days(
            settings.DATA_CLEANUP_DOWNLOAD_FAILURE_DAYS
        ),
    )


def _normalize_days(retention_days: Any) -> int:
    """把配置保留期规范为非负整数，非法值按关闭单表清理处理。"""
    try:
        normalized_days = int(retention_days or 0)
    except (TypeError, ValueError):
        return 0
    return max(normalized_days, 0)
