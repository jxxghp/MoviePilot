from datetime import datetime
from typing import List, Optional, Tuple

from app.db.base import DbOper
from app.db.models.transferpending import TransferPending


class TransferPendingOper(DbOper):
    """
    待整理文件登记管理。

    保存稳定任务身份、存储、源文件路径和准入状态，用于在进程重启后把没走完
    整理链的文件重新送回去，避免挂载故障重启后永久漏件。旧版路径登记接口继续
    保留，供插件和兼容调用方使用。
    """

    def register(self, storage: str, src_path: str) -> Optional[TransferPending]:
        """
        登记一个待整理文件。
        :param storage: 存储
        :param src_path: 源文件路径
        :return: 登记记录
        """
        now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return self._execute_sync_write(
            lambda session: TransferPending.register(
                session,
                storage=storage,
                src_path=src_path,
                now_time=now_time,
            )
        )

    def stage_admit(self, *, task_id: str, storage: str, src_path: str,
                    state: str, now_time: str) -> Optional[TransferPending]:
        """
        在当前会话中暂存一条持久接纳记录。

        适配器应传入显式 Session，使提交与回滚仍由应用用例对应的 UoW 管理。
        :param task_id: 任务标识
        :param storage: 存储
        :param src_path: 源文件路径
        :param state: 持久状态
        :param now_time: 当前时间
        :return: 接纳记录
        """
        return self._execute_sync_write(
            lambda session: TransferPending.stage_admit(
                session,
                task_id=task_id,
                storage=storage,
                src_path=src_path,
                state=state,
                now_time=now_time,
            )
        )

    def list_by_state(self, *, state: str,
                      limit: Optional[int] = 5000) -> List[TransferPending]:
        """
        使用当前会话列出指定状态记录。
        :param state: 持久状态
        :param limit: 单次读取上限
        :return: ORM 接纳记录列表
        """
        return self._execute_sync_query(
            lambda session: TransferPending.list_by_state(
                session,
                state=state,
                limit=limit,
            )
        ) or []

    def get_by_identity(self, *, storage: str,
                        src_path: str) -> Optional[TransferPending]:
        """
        使用当前会话按存储与源路径查询接纳记录。
        :param storage: 存储
        :param src_path: 源文件路径
        :return: 接纳记录
        """
        return self._execute_sync_query(
            lambda session: TransferPending.get_by_identity(
                session,
                storage=storage,
                src_path=src_path,
            )
        )

    def stage_record_enqueue_failure(self, *, task_id: str, error: str,
                                     now_time: str) -> int:
        """
        在当前会话中暂存最近一次入队失败。
        :param task_id: 任务标识
        :param error: 失败原因
        :param now_time: 当前时间
        :return: 更新的记录数
        """
        return self._execute_sync_write(
            lambda session: TransferPending.record_enqueue_failure(
                session,
                task_id=task_id,
                error=error,
                now_time=now_time,
            )
        )

    def stage_discard_task(self, *, task_id: str) -> int:
        """
        在当前会话中暂存按任务标识删除接纳记录。
        :param task_id: 任务标识
        :return: 删除的记录数
        """
        return self._execute_sync_write(
            lambda session: TransferPending.discard_task(
                session,
                task_id=task_id,
            )
        )

    def discard(self, storage: str, src_path: str) -> int:
        """
        注销一个待整理文件登记。
        :param storage: 存储
        :param src_path: 源文件路径
        :return: 删除的记录数
        """
        return self._execute_sync_write(
            lambda session: TransferPending.discard(
                session,
                storage=storage,
                src_path=src_path,
            )
        )

    def list_all(self, limit: Optional[int] = 5000) -> List[Tuple[str, str]]:
        """
        列出全部待整理登记，供启动回放使用。

        返回纯元组而不是 ORM 实例：回放发生在会话之外，ORM 实例脱离 session
        后访问属性会触发 DetachedInstanceError。
        :param limit: 单次回放上限
        :return: (存储, 源文件路径) 列表
        """
        items = self._execute_sync_query(
            lambda session: TransferPending.list_all(session, limit=limit)
        )
        return [
            (item.storage, item.src_path)
            for item in items or []
            if item and item.storage and item.src_path
        ]

    def clear(self) -> int:
        """
        清空全部待整理登记。
        :return: 删除的记录数
        """
        return self._execute_sync_write(TransferPending.clear)
