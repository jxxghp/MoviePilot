"""兼容旧 ``app.db.transferpending_oper`` 的无 Session 数据访问接口。"""

import hashlib
import json
from datetime import datetime
from typing import Any, List, Optional, Tuple
from uuid import uuid4

from sqlalchemy import column, delete, exists, select, table

from app.db.base import DbOper, execute_dml
from app.db.models.transferpending import TransferPending as _TransferPending

_TRANSFER_EXECUTION_STEP = table("transferexecutionstep", column("task_id"))
_TRANSFER_HISTORY = table("transferhistory", column("transfer_task_id"))


def _legacy_planning_payload(storage: str, src_path: str) -> dict[str, Any]:
    """只在旧登记 ABI 内构造可由新状态机保守重规划的最小输入。"""
    return {
        "schema_version": 1,
        "source_fileitem": {"storage": storage, "path": src_path},
        "meta": None,
        "mediainfo": None,
        "target_directory": None,
        "target_storage": None,
        "target_path": None,
        "requested_transfer_type": None,
        "media_source": None,
        "media_id": None,
        "media_type": None,
        "need_scrape": False,
        "need_rename": True,
        "need_notify": True,
        "overwrite_mode": None,
        "episodes_info": [],
        "preview": False,
        "options": {"legacy_replan": True},
    }


def _planning_fingerprint(payload: dict[str, Any]) -> str:
    """生成兼容登记输入与 canonical DTO 一致的规范指纹。"""
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_legacy_delete_predicates() -> tuple[Any, ...]:
    """只允许旧接口删除从未 claim、执行或结算的新鲜登记。"""
    return (
        _TransferPending.lease_owner.is_(None),
        _TransferPending.lease_token.is_(None),
        _TransferPending.lease_expires_at.is_(None),
        _TransferPending.heartbeat_at.is_(None),
        _TransferPending.attempt_count == 0,
        _TransferPending.execution_state == "not_started",
        _TransferPending.execution_version.is_(None),
        _TransferPending.execution_payload.is_(None),
        _TransferPending.execution_fingerprint.is_(None),
        _TransferPending.retry_generation == 0,
        _TransferPending.retry_count == 0,
        _TransferPending.retry_due_at.is_(None),
        _TransferPending.settlement_revision == 0,
        _TransferPending.terminal_history_id.is_(None),
        _TransferPending.last_error.is_(None),
        ~exists(
            select(_TRANSFER_EXECUTION_STEP.c.task_id).where(
                _TRANSFER_EXECUTION_STEP.c.task_id == _TransferPending.task_id
            )
        ),
        ~exists(
            select(_TRANSFER_HISTORY.c.transfer_task_id).where(
                _TRANSFER_HISTORY.c.transfer_task_id == _TransferPending.task_id
            )
        ),
    )


class TransferPendingOper(DbOper):
    """
    保留旧待整理登记 ABI，并对执行租约实施兼容写 fencing。

    本类只由精确旧导入映射加载。宿主整理链仍使用 Application Port 和显式
    Session 的 canonical Oper；旧删除入口只能处理无任何执行证据的新鲜登记。
    """

    def register(self, storage: str, src_path: str) -> Optional[_TransferPending]:
        """
        按旧签名登记待整理文件，重复登记保持原任务和租约不变。

        :param storage: 存储
        :param src_path: 源文件路径
        :return: 登记记录
        """
        now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        planning_input = _legacy_planning_payload(storage, src_path)
        return self._execute_sync_write(
            lambda session: _TransferPending.stage_admit(
                session,
                task_id=uuid4().hex,
                storage=storage,
                src_path=src_path,
                state="accepted",
                now_time=now_time,
                input_version=1,
                planning_input=planning_input,
                input_fingerprint=_planning_fingerprint(planning_input),
            )
        )

    def list_by_state(
            self,
            *,
            state: str,
            limit: Optional[int] = 5000,
    ) -> List[_TransferPending]:
        """
        按旧签名和登记顺序列出指定状态记录。

        :param state: 持久状态
        :param limit: 单次读取上限
        :return: ORM 接纳记录列表
        """
        if not state:
            return []
        return self._execute_sync_query(
            lambda session: list(session.execute(
                select(_TransferPending)
                .where(_TransferPending.state == state)
                .order_by(_TransferPending.created_at.asc(), _TransferPending.id.asc())
                .limit(limit)
            ).scalars().all())
        )

    def list_by_states(
            self,
            *,
            states: tuple[str, ...],
            limit: Optional[int] = 5000,
    ) -> List[_TransferPending]:
        """
        按旧签名和登记顺序列出多个状态记录。

        :param states: 持久状态集合
        :param limit: 单次读取上限
        :return: ORM 接纳记录列表
        """
        if not states:
            return []
        return self._execute_sync_query(
            lambda session: list(session.execute(
                select(_TransferPending)
                .where(_TransferPending.state.in_(states))
                .order_by(_TransferPending.created_at.asc(), _TransferPending.id.asc())
                .limit(limit)
            ).scalars().all())
        )

    def get_by_identity(
            self,
            *,
            storage: str,
            src_path: str,
    ) -> Optional[_TransferPending]:
        """
        按旧签名查询指定存储与源路径的登记。

        :param storage: 存储
        :param src_path: 源文件路径
        :return: 登记记录
        """
        return self._execute_sync_query(
            lambda session: _TransferPending.get_by_identity(
                session,
                storage=storage,
                src_path=src_path,
            )
        )

    def get_by_task_id(self, *, task_id: str) -> Optional[_TransferPending]:
        """
        按旧签名查询稳定任务标识对应的登记。

        :param task_id: 稳定任务标识
        :return: 登记记录
        """
        return self._execute_sync_query(
            lambda session: _TransferPending.get_by_task_id(
                session,
                task_id=task_id,
            )
        )

    def discard(self, storage: str, src_path: str) -> int:
        """
        按旧签名删除从未 claim 且没有执行证据的指定登记。

        任何租约、重试、步骤或终态证据都归 canonical 状态机所有；即使租约
        已经过期，旧插件也不得越权代替恢复调度器删除。
        :param storage: 存储
        :param src_path: 源文件路径
        :return: 删除的记录数
        """
        if not storage or not src_path:
            return 0
        return self._execute_sync_write(
            lambda session: execute_dml(
                session,
                delete(_TransferPending).where(
                    _TransferPending.storage == storage,
                    _TransferPending.src_path == src_path,
                    *_safe_legacy_delete_predicates(),
                ),
                execution_options={"synchronize_session": False},
            )
        )

    def list_all(self, limit: Optional[int] = 5000) -> List[Tuple[str, str]]:
        """
        按旧返回形态列出全部待整理路径。

        :param limit: 单次读取上限
        :return: ``(存储, 源文件路径)`` 列表
        """
        rows = self._execute_sync_query(
            lambda session: session.execute(
                select(_TransferPending.storage, _TransferPending.src_path)
                .order_by(_TransferPending.created_at.asc(), _TransferPending.id.asc())
                .limit(limit)
            ).all()
        )
        return [
            (storage, src_path)
            for storage, src_path in rows
            if storage and src_path
        ]

    def clear(self) -> int:
        """
        清空从未 claim 且没有执行证据的登记，保留状态机拥有的任务。

        :return: 删除的记录数
        """
        return self._execute_sync_write(
            lambda session: execute_dml(
                session,
                delete(_TransferPending).where(
                    *_safe_legacy_delete_predicates(),
                ),
                execution_options={"synchronize_session": False},
            )
        )


__all__ = ["TransferPendingOper"]
