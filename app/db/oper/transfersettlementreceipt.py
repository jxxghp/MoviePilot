"""整理任务终态结算回执的事务内数据访问。"""

from typing import Optional

from app.db.base import DbOper
from app.db.models.transfersettlementreceipt import TransferSettlementReceipt


class TransferSettlementReceiptOper(DbOper):
    """在调用方 Session 内读取和推进 durable 结算回执。"""

    def get_latest_by_task_id(
            self,
            *,
            task_id: str,
    ) -> Optional[TransferSettlementReceipt]:
        """按稳定任务标识读取最新回执。"""
        return TransferSettlementReceipt.get_latest_by_task_id(
            self._db,
            task_id=task_id,
        )

    def get_by_identity(
            self,
            *,
            task_id: str,
            execution_fingerprint: str,
            lease_token: str,
            outcome: str,
    ) -> Optional[TransferSettlementReceipt]:
        """按原始执行身份读取不可变回执。"""
        return TransferSettlementReceipt.get_by_identity(
            self._db,
            task_id=task_id,
            execution_fingerprint=execution_fingerprint,
            lease_token=lease_token,
            outcome=outcome,
        )

    def stage_append(
            self,
            *,
            task_id: str,
            history_id: int,
            settlement_revision: int,
            outcome: str,
            execution_fingerprint: str,
            lease_token: str,
            history_status: bool,
            src: Optional[str],
            src_storage: Optional[str],
            pending_deleted: bool,
            error: Optional[str],
            settled_at: str,
    ) -> TransferSettlementReceipt:
        """在调用方事务内按连续修订追加任务结算回执。"""
        return TransferSettlementReceipt.stage_append(
            self._db,
            task_id=task_id,
            history_id=history_id,
            settlement_revision=settlement_revision,
            outcome=outcome,
            execution_fingerprint=execution_fingerprint,
            lease_token=lease_token,
            history_status=history_status,
            src=src,
            src_storage=src_storage,
            pending_deleted=pending_deleted,
            error=error,
            settled_at=settled_at,
        )
