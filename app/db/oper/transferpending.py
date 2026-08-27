from typing import Any, Optional

from app.db.base import DbOper
from app.db.models.transferpending import TransferPending


class TransferPendingOper(DbOper):
    """
    待整理文件登记管理。

    保存稳定任务身份、存储、源文件路径和准入状态，用于在进程重启后把没走完
    整理链的文件重新送回去，避免挂载故障重启后永久漏件。
    """

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
            lease_token: str,
            now_time: str,
            updated_at: str,
    ) -> int:
        """
        在当前会话中以输入指纹为条件暂存完整计划检查点。
        :param task_id: 稳定任务标识
        :param input_fingerprint: 规划输入规范 JSON 指纹
        :param checkpoint_version: 检查点格式版本
        :param checkpoint_payload: 完整有序计划 JSON
        :param source_states: 允许执行 CAS 的起始状态
        :param target_state: 检查点提交后的目标状态
        :param lease_token: 当前且未过期的租约令牌
        :param now_time: 用于租约 fencing 的当前 UTC 时间
        :param updated_at: 与既有业务审计字段一致的宿主本地时间
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
                lease_token=lease_token,
                now_time=now_time,
                updated_at=updated_at,
            )
        )

    def stage_record_planning_failure(self, *, task_id: str, lease_token: str,
                                      error: str, now_time: str,
                                      updated_at: str) -> int:
        """
        在当前会话中记录规划失败并保持任务处于接纳态。
        :param task_id: 稳定任务标识
        :param lease_token: 当前且未过期的租约令牌
        :param error: 失败原因
        :param now_time: 用于租约 fencing 的当前 UTC 时间
        :param updated_at: 与既有业务审计字段一致的宿主本地时间
        :return: 更新的记录数
        """
        return self._execute_sync_write(
            lambda session: TransferPending.record_planning_failure(
                session,
                task_id=task_id,
                lease_token=lease_token,
                error=error,
                now_time=now_time,
                updated_at=updated_at,
            )
        )

    def list_claimable_candidates(
            self,
            *,
            states: tuple[str, ...],
            now_time: str,
            limit: int,
            after_cursor: Optional[tuple[str, int]] = None,
    ) -> list[tuple[str, str, int]]:
        """
        使用当前会话按稳定游标读取未租用或已过期的恢复候选。

        :param states: 可恢复业务状态
        :param now_time: 当前 UTC 时间
        :param limit: 候选数量上限
        :param after_cursor: 上一页最后一条的规范登记时间与主键
        :return: 任务标识、规范登记时间与主键组成的稳定游标列表
        """
        return self._execute_sync_query(
            lambda session: TransferPending.list_claimable_candidates(
                session,
                states=states,
                now_time=now_time,
                limit=limit,
                after_cursor=after_cursor,
            )
        ) or []

    def stage_claim_task(
            self,
            *,
            task_id: str,
            states: tuple[str, ...],
            owner_id: str,
            lease_token: str,
            now_time: str,
            lease_expires_at: str,
            updated_at: str,
    ) -> int:
        """
        使用当前会话以未租或过期条件竞争一个新租约。

        :param task_id: 稳定任务标识
        :param states: 可恢复业务状态
        :param owner_id: 新租约拥有者
        :param lease_token: 新租约唯一令牌
        :param now_time: 当前 UTC 时间
        :param lease_expires_at: 新租约到期时间
        :param updated_at: 与既有业务审计字段一致的宿主本地时间
        :return: 更新的记录数
        """
        return self._execute_sync_write(
            lambda session: TransferPending.claim_task(
                session,
                task_id=task_id,
                states=states,
                owner_id=owner_id,
                lease_token=lease_token,
                now_time=now_time,
                lease_expires_at=lease_expires_at,
                updated_at=updated_at,
            )
        )

    def stage_record_projection_failure(
            self,
            *,
            task_id: str,
            states: tuple[str, ...],
            error: str,
            now_time: str,
            updated_at: str,
    ) -> int:
        """
        使用当前会话按无有效租约和诊断变化条件记录投影损坏。

        :param task_id: 稳定任务标识
        :param states: 可恢复业务状态
        :param error: 可持久化的稳定诊断文本
        :param now_time: 当前 UTC 租约时间
        :param updated_at: 宿主本地业务审计时间
        :return: 更新的记录数
        """
        return self._execute_sync_write(
            lambda session: TransferPending.record_projection_failure(
                session,
                task_id=task_id,
                states=states,
                error=error,
                now_time=now_time,
                updated_at=updated_at,
            )
        )

    def stage_heartbeat(
            self,
            *,
            task_id: str,
            lease_token: str,
            now_time: str,
            lease_expires_at: str,
    ) -> int:
        """
        使用当前会话以当前未过期 token 延长租约。

        :param task_id: 稳定任务标识
        :param lease_token: 当前租约令牌
        :param now_time: 当前 UTC 时间
        :param lease_expires_at: 新租约到期时间
        :return: 更新的记录数
        """
        return self._execute_sync_write(
            lambda session: TransferPending.heartbeat(
                session,
                task_id=task_id,
                lease_token=lease_token,
                now_time=now_time,
                lease_expires_at=lease_expires_at,
            )
        )

    def stage_release_claim(
            self,
            *,
            task_id: str,
            lease_token: str,
            error: Optional[str],
            now_time: str,
            updated_at: str,
    ) -> int:
        """
        使用当前会话按未过期 token 释放租约并记录本次错误。

        :param task_id: 稳定任务标识
        :param lease_token: 当前租约令牌
        :param error: 本次执行错误，成功释放时为空
        :param now_time: 当前 UTC 时间
        :param updated_at: 与既有业务审计字段一致的宿主本地时间
        :return: 更新的记录数
        """
        return self._execute_sync_write(
            lambda session: TransferPending.release_claim(
                session,
                task_id=task_id,
                lease_token=lease_token,
                error=error,
                now_time=now_time,
                updated_at=updated_at,
            )
        )

    def stage_discard_claimed(
            self,
            *,
            task_id: str,
            lease_token: str,
            now_time: str,
    ) -> int:
        """
        使用当前会话按当前未过期 token 删除终态任务。

        :param task_id: 稳定任务标识
        :param lease_token: 当前租约令牌
        :param now_time: 当前 UTC 时间
        :return: 删除的记录数
        """
        return self._execute_sync_write(
            lambda session: TransferPending.discard_claimed(
                session,
                task_id=task_id,
                lease_token=lease_token,
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
