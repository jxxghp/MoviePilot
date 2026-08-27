"""兼容旧 ``app.db.transferpending_oper`` 的无 Session 数据访问接口。"""

from datetime import datetime
from typing import List, Optional, Tuple
from uuid import uuid4

from sqlalchemy import delete, select

from app.db.base import DbOper, execute_dml
from app.db.models.transferpending import TransferPending as _TransferPending


class TransferPendingOper(DbOper):
    """
    保留旧待整理登记 ABI，并对执行租约实施兼容写 fencing。

    本类只由精确旧导入映射加载。宿主整理链仍使用 Application Port 和显式
    Session 的 canonical Oper；旧删除入口只能处理从未取得租约的记录。
    """

    def register(self, storage: str, src_path: str) -> Optional[_TransferPending]:
        """
        按旧签名登记待整理文件，重复登记保持原任务和租约不变。

        :param storage: 存储
        :param src_path: 源文件路径
        :return: 登记记录
        """
        now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return self._execute_sync_write(
            lambda session: _TransferPending.stage_admit(
                session,
                task_id=uuid4().hex,
                storage=storage,
                src_path=src_path,
                state="accepted",
                now_time=now_time,
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
        按旧签名删除未 claim 的指定登记。

        任何带 token 的记录都由当前租约拥有者通过 fenced canonical API 收口；
        即使租约已经过期，旧插件也不得越权代替恢复调度器删除。
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
                    _TransferPending.lease_token.is_(None),
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
        清空全部未 claim 登记，保留任何带租约 token 的任务。

        :return: 删除的记录数
        """
        return self._execute_sync_write(
            lambda session: execute_dml(
                session,
                delete(_TransferPending).where(
                    _TransferPending.lease_token.is_(None),
                ),
                execution_options={"synchronize_session": False},
            )
        )


__all__ = ["TransferPendingOper"]
