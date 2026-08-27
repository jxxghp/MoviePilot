from datetime import datetime
from typing import Any, List, Optional, Tuple

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
                    state: str, now_time: str, input_version: int = 1,
                    planning_input: Optional[dict[str, Any]] = None,
                    input_fingerprint: Optional[str] = None) -> Optional[TransferPending]:
        """
        在当前会话中暂存一条持久接纳记录。

        适配器应传入显式 Session，使提交与回滚仍由应用用例对应的 UoW 管理。
        :param task_id: 任务标识
        :param storage: 存储
        :param src_path: 源文件路径
        :param state: 持久状态
        :param now_time: 当前时间
        :param input_version: 规划输入格式版本
        :param planning_input: 版本化规划输入 JSON
        :param input_fingerprint: 规划输入规范 JSON 指纹
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
                input_version=input_version,
                planning_input=planning_input,
                input_fingerprint=input_fingerprint,
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

    def list_by_states(self, *, states: tuple[str, ...],
                       limit: Optional[int] = 5000) -> List[TransferPending]:
        """
        使用当前会话列出多个可恢复状态的记录。
        :param states: 允许恢复的状态集合
        :param limit: 单次读取上限
        :return: ORM 接纳记录列表
        """
        return self._execute_sync_query(
            lambda session: TransferPending.list_by_states(
                session,
                states=states,
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

    def get_by_task_id(self, *, task_id: str) -> Optional[TransferPending]:
        """
        使用当前会话按稳定任务标识查询接纳记录。
        :param task_id: 稳定任务标识
        :return: 接纳记录
        """
        return self._execute_sync_query(
            lambda session: TransferPending.get_by_task_id(
                session,
                task_id=task_id,
            )
        )

    def stage_checkpoint_plan(
            self,
            *,
            task_id: str,
            input_fingerprint: str,
            checkpoint_version: int,
            checkpoint_payload: dict[str, Any],
            source_states: tuple[str, ...],
            target_state: str,
            now_time: str,
    ) -> int:
        """
        在当前会话中以输入指纹为条件暂存完整计划检查点。
        :param task_id: 稳定任务标识
        :param input_fingerprint: 规划输入规范 JSON 指纹
        :param checkpoint_version: 检查点格式版本
        :param checkpoint_payload: 完整有序计划 JSON
        :param source_states: 允许执行 CAS 的起始状态
        :param target_state: 检查点提交后的目标状态
        :param now_time: 当前时间
        :return: 更新的记录数
        """
        return self._execute_sync_write(
            lambda session: TransferPending.checkpoint_plan(
                session,
                task_id=task_id,
                input_fingerprint=input_fingerprint,
                checkpoint_version=checkpoint_version,
                checkpoint_payload=checkpoint_payload,
                source_states=source_states,
                target_state=target_state,
                now_time=now_time,
            )
        )

    def stage_record_planning_failure(self, *, task_id: str, error: str,
                                      now_time: str) -> int:
        """
        在当前会话中记录规划失败并保持任务处于接纳态。
        :param task_id: 稳定任务标识
        :param error: 失败原因
        :param now_time: 当前时间
        :return: 更新的记录数
        """
        return self._execute_sync_write(
            lambda session: TransferPending.record_planning_failure(
                session,
                task_id=task_id,
                error=error,
                now_time=now_time,
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
