import hashlib
import json
from datetime import datetime
from typing import Any, List, Optional, cast
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, execute_dml, get_id_column


def _legacy_planning_payload(storage: str, src_path: str) -> dict[str, Any]:
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
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _default_planning_payload(context: Any) -> dict[str, Any]:
    params = context.get_current_parameters()
    return _legacy_planning_payload(
        params.get("storage", ""),
        params.get("src_path", ""),
    )


def _default_planning_fingerprint(context: Any) -> str:
    params = context.get_current_parameters()
    payload = params.get("planning_input") or _legacy_planning_payload(
        params.get("storage", ""),
        params.get("src_path", ""),
    )
    return _planning_fingerprint(payload)


class TransferPending(Base):
    """
    待整理文件登记。

    整理队列是纯内存的 queue.Queue：进程一旦重启（挂载挂死后的人工重启、版本
    升级、OOM、宿主重启），队列里的任务会连同「这些文件还没整理」这个事实一起
    蒸发。而已经稳定落地的文件不会再产生任何监控事件，也不会有新的补偿扫描起点
    ——结果就是永久漏件，只能靠人工比对补整理。

    准入时保存版本化规划输入和指纹；纯规划完成后以同一行原子保存完整有序计划并
    推进到 planned。重启恢复可直接消费已规划路径，避免再次触发 rename 等插件事件。
    所有执行期 mutation 都以稳定任务身份和租约 token 进行 CAS fencing。
    """

    id = get_id_column()
    # 稳定任务标识
    task_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=lambda: uuid4().hex
    )
    # 存储
    storage: Mapped[str] = mapped_column(String, nullable=False)
    # 源文件路径
    src_path: Mapped[str] = mapped_column(String, nullable=False)
    # 登记时间
    created_at: Mapped[Optional[str]] = mapped_column(String)
    # 持久状态
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    # 最后更新时间
    updated_at: Mapped[str] = mapped_column(
        String(40), nullable=False,
        default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    # 最近一次入队失败原因
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    # 规划输入格式版本
    input_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 版本化规划输入 JSON
    planning_input: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=_default_planning_payload
    )
    # 规划输入规范 JSON 的 SHA-256 指纹
    input_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, default=_default_planning_fingerprint
    )
    # 完整计划格式版本，尚未规划时为空
    checkpoint_version: Mapped[Optional[int]] = mapped_column(Integer)
    # 完整有序计划 JSON，尚未规划时为空
    checkpoint_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    # 规划完成时间
    planned_at: Mapped[Optional[str]] = mapped_column(String(40))
    # 当前租约拥有者
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128))
    # 当前租约的唯一防陈旧令牌
    lease_token: Mapped[Optional[str]] = mapped_column(String(64))
    # 当前租约的 UTC 到期时间
    lease_expires_at: Mapped[Optional[str]] = mapped_column(String(40))
    # 最近一次成功 claim 或 heartbeat 的 UTC 时间
    heartbeat_at: Mapped[Optional[str]] = mapped_column(String(40))
    # 真正取得新 token 的累计次数
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 与规划状态正交的执行状态
    execution_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_started"
    )
    # 聚合执行检查点格式版本
    execution_version: Mapped[Optional[int]] = mapped_column(Integer)
    # 可独立重放终态结算的聚合执行结果
    execution_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    # 聚合执行结果规范 JSON 的 SHA-256 指纹
    execution_fingerprint: Mapped[Optional[str]] = mapped_column(String(64))
    # 每次进入 retry_wait 都递增的调度世代
    retry_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 已持久提交的步骤重试次数
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 下一次允许 claim 的 UTC 时间
    retry_due_at: Mapped[Optional[str]] = mapped_column(String(40))
    # 最近一次终态失败重试请求身份
    retry_requested_by: Mapped[Optional[str]] = mapped_column(String(128))
    # 最近一次终态失败重试请求原因
    retry_reason: Mapped[Optional[str]] = mapped_column(Text)
    # 已完成终态结算的单调版本
    settlement_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 失败终态保留的整理历史标识
    terminal_history_id: Mapped[Optional[int]] = mapped_column(Integer)
    # 人工判定的单调审计版本
    manual_review_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # 最近一次人工判定时间
    reviewed_at: Mapped[Optional[str]] = mapped_column(String(40))
    # 最近一次人工判定操作者
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(128))
    # 最近一次人工判定原因
    review_reason: Mapped[Optional[str]] = mapped_column(Text)
    # 最近一次人工判定结论
    review_decision: Mapped[Optional[str]] = mapped_column(String(32))

    __table_args__ = (
        # 同一个文件重复入队只保留一条，回放时不会重复送入整理链
        Index("ux_transferpending_storage_path", "storage", "src_path", unique=True),
        # 恢复主查询按状态过滤、登记时间与主键稳定排序
        Index(
            "ix_transferpending_state_created",
            "state",
            "created_at",
            "id",
        ),
        # 恢复调度按业务状态和租约到期时间筛选可接管任务
        Index(
            "ix_transferpending_recovery_lease",
            "state",
            "lease_expires_at",
            "created_at",
            "id",
        ),
        Index(
            "ix_transferpending_execution_due",
            "execution_state",
            "retry_due_at",
            "state",
            "created_at",
            "id",
        ),
        UniqueConstraint("task_id", name="uq_transferpending_task_id"),
    )

    @classmethod
    def stage_admit(cls, db: Session, *, task_id: str, storage: str,
                    src_path: str, state: str,
                    now_time: str, input_version: int = 1,
                    planning_input: Optional[dict[str, Any]] = None,
                    input_fingerprint: Optional[str] = None) -> Optional["TransferPending"]:
        """
        在调用方会话中暂存一条持久接纳记录。

        相同存储与路径已存在时返回原记录，确保重复监控事件复用同一个任务标识。
        :param db: 数据库会话
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
        if not task_id or not storage or not src_path or not state:
            return None
        pending = db.execute(
            select(cls).where(cls.storage == storage, cls.src_path == src_path)
        ).scalars().first()
        if pending:
            return cast("TransferPending", pending)
        effective_input = planning_input or _legacy_planning_payload(storage, src_path)
        effective_fingerprint = input_fingerprint or _planning_fingerprint(effective_input)
        pending = cls(
            task_id=task_id,
            storage=storage,
            src_path=src_path,
            state=state,
            created_at=now_time,
            updated_at=now_time,
            input_version=input_version,
            planning_input=effective_input,
            input_fingerprint=effective_fingerprint,
        )
        db.add(pending)
        return pending

    @classmethod
    def get_by_identity(cls, db: Session, *, storage: str,
                        src_path: str) -> Optional["TransferPending"]:
        """
        按存储与源路径查询一条持久接纳记录。
        :param db: 数据库会话
        :param storage: 存储
        :param src_path: 源文件路径
        :return: 接纳记录
        """
        if not storage or not src_path:
            return None
        return cast(
            Optional["TransferPending"],
            db.execute(
                select(cls).where(
                    cls.storage == storage,
                    cls.src_path == src_path,
                )
            ).scalars().first(),
        )

    @classmethod
    def get_by_task_id(cls, db: Session, *, task_id: str) -> Optional["TransferPending"]:
        """
        按稳定任务标识查询一条持久登记。
        :param db: 数据库会话
        :param task_id: 稳定任务标识
        :return: 接纳记录
        """
        if not task_id:
            return None
        return cast(
            Optional["TransferPending"],
            db.execute(select(cls).where(cls.task_id == task_id)).scalars().first(),
        )

    @classmethod
    def list_claimable_candidates(
            cls,
            db: Session,
            *,
            states: tuple[str, ...],
            now_time: str,
            limit: int,
            after_cursor: Optional[tuple[str, int]] = None,
    ) -> List[tuple[str, str, int]]:
        """
        按稳定游标列出未租用或租约已过期的候选任务。

        返回候选不等于取得租约；调用方必须继续执行带相同过期条件的 claim CAS，
        并以受影响行数决定竞争结果。
        :param db: 数据库会话
        :param states: 可恢复业务状态
        :param now_time: 当前 UTC 时间
        :param limit: 候选数量上限
        :param after_cursor: 上一页最后一条的规范登记时间与主键
        :return: 任务标识、规范登记时间与主键组成的稳定游标列表
        """
        if not states or not now_time or limit <= 0:
            return []
        cursor_created_at = func.coalesce(cls.created_at, "")
        statement = select(cls.task_id, cursor_created_at, cls.id).where(
            cls.state.in_(states),
            cls.execution_state.in_((
                "not_started",
                "running",
                "retry_wait",
                "settling",
            )),
            or_(
                cls.execution_state != "retry_wait",
                and_(
                    cls.retry_due_at.is_not(None),
                    cls.retry_due_at <= now_time,
                ),
            ),
            or_(
                cls.lease_token.is_(None),
                cls.lease_expires_at.is_(None),
                cls.lease_expires_at <= now_time,
            ),
        )
        if after_cursor is not None:
            after_created_at, after_id = after_cursor
            statement = statement.where(or_(
                cursor_created_at > after_created_at,
                and_(
                    cursor_created_at == after_created_at,
                    cls.id > after_id,
                ),
            ))
        rows = db.execute(
            statement
            .order_by(cursor_created_at.asc(), cls.id.asc())
            .limit(limit)
        ).all()
        return [
            (task_id, created_at or "", int(row_id))
            for task_id, created_at, row_id in rows
        ]

    @classmethod
    def claim_task(
            cls,
            db: Session,
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
        以未租用或租约已过期为条件原子取得任务租约。

        :param db: 数据库会话
        :param task_id: 稳定任务标识
        :param states: 允许 claim 的业务状态
        :param owner_id: 新租约拥有者
        :param lease_token: 新租约唯一令牌
        :param now_time: 当前 UTC 时间
        :param lease_expires_at: 新租约到期时间
        :param updated_at: 与既有业务审计字段一致的宿主本地时间
        :return: 更新的记录数，1 表示赢得竞争
        """
        if not all((
                task_id,
                states,
                owner_id,
                lease_token,
                now_time,
                lease_expires_at,
                updated_at,
        )):
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.state.in_(states),
                cls.execution_state.in_((
                    "not_started",
                    "running",
                    "retry_wait",
                    "settling",
                )),
                or_(
                    cls.execution_state != "retry_wait",
                    and_(
                        cls.retry_due_at.is_not(None),
                        cls.retry_due_at <= now_time,
                    ),
                ),
                or_(
                    cls.lease_token.is_(None),
                    cls.lease_expires_at.is_(None),
                    cls.lease_expires_at <= now_time,
                ),
            )
            .values(
                lease_owner=owner_id,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                heartbeat_at=now_time,
                attempt_count=cls.attempt_count + 1,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def stage_execution_running(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以有效租约把可执行任务推进或保持为 running。"""
        if not all((task_id, lease_token, now_utc, updated_at)):
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.lease_token == lease_token,
                cls.lease_expires_at.is_not(None),
                cls.lease_expires_at > now_utc,
                cls.execution_state.in_(("not_started", "running", "retry_wait")),
            )
            .values(
                execution_state="running",
                retry_due_at=None,
                last_error=None,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def defer_execution(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            error: str,
            retry_due_at: str,
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以有效租约进入 retry_wait，并原子释放当前租约。"""
        if not all((task_id, lease_token, error, retry_due_at, now_utc, updated_at)):
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.state == "planned",
                cls.checkpoint_version.is_not(None),
                cls.checkpoint_payload.is_not(None),
                cls.execution_state == "running",
                cls.lease_token == lease_token,
                cls.lease_expires_at.is_not(None),
                cls.lease_expires_at > now_utc,
            )
            .values(
                execution_state="retry_wait",
                retry_generation=cls.retry_generation + 1,
                retry_count=cls.retry_count + 1,
                retry_due_at=retry_due_at,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
                last_error=error,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def mark_execution_manual_review(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            error: str,
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以有效租约隔离执行结果未知的任务，并释放自动调度租约。"""
        if not all((task_id, lease_token, error, now_utc, updated_at)):
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.execution_state.in_(("not_started", "running", "retry_wait")),
                cls.lease_token == lease_token,
                cls.lease_expires_at.is_not(None),
                cls.lease_expires_at > now_utc,
            )
            .values(
                execution_state="manual_review",
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
                last_error=error,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def checkpoint_execution(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            execution_version: int,
            execution_payload: dict[str, Any],
            execution_fingerprint: str,
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以有效租约保存可重放执行检查点并进入 settling。"""
        if not all((
                task_id,
                lease_token,
                execution_version,
                execution_payload,
                execution_fingerprint,
                now_utc,
                updated_at,
        )):
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.state == "planned",
                cls.checkpoint_version.is_not(None),
                cls.checkpoint_payload.is_not(None),
                cls.execution_state == "running",
                cls.lease_token == lease_token,
                cls.lease_expires_at.is_not(None),
                cls.lease_expires_at > now_utc,
            )
            .values(
                execution_state="settling",
                execution_version=execution_version,
                execution_payload=execution_payload,
                execution_fingerprint=execution_fingerprint,
                retry_due_at=None,
                last_error=None,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def checkpoint_exhausted_failure(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            execution_version: int,
            execution_payload: dict[str, Any],
            execution_fingerprint: str,
            error: str,
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以有效 lease 保存预算耗尽失败检查点并保持租约进入 settling。"""
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.state == "planned",
                cls.checkpoint_version.is_not(None),
                cls.checkpoint_payload.is_not(None),
                cls.execution_state == "running",
                cls.lease_token == lease_token,
                cls.lease_expires_at.is_not(None),
                cls.lease_expires_at > now_utc,
            )
            .values(
                execution_state="settling",
                execution_version=execution_version,
                execution_payload=execution_payload,
                execution_fingerprint=execution_fingerprint,
                retry_due_at=None,
                last_error=error,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def request_execution_retry(
            cls,
            db: Session,
            *,
            task_id: str,
            reason: str,
            requested_by: str,
            retry_due_at: str,
            updated_at: str,
    ) -> int:
        """仅将无租约 FAILED 任务 CAS 为立即到期的 retry_wait。"""
        if not all((task_id, reason, requested_by, retry_due_at, updated_at)):
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.execution_state == "failed",
                cls.lease_token.is_(None),
            )
            .values(
                execution_state="retry_wait",
                retry_generation=cls.retry_generation + 1,
                retry_due_at=retry_due_at,
                retry_requested_by=requested_by,
                retry_reason=reason,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def resolve_manual_review(
            cls,
            db: Session,
            *,
            task_id: str,
            decision: str,
            actor: str,
            reason: str,
            retry_due_at: str,
            updated_at: str,
    ) -> int:
        """无 lease 地 CAS 提交人工判定审计并交回 retry_wait 调度。"""
        if decision not in {"not_applied", "applied"}:
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.execution_state == "manual_review",
                cls.lease_token.is_(None),
                cls.lease_owner.is_(None),
            )
            .values(
                execution_state="retry_wait",
                retry_generation=cls.retry_generation + 1,
                retry_due_at=retry_due_at,
                manual_review_revision=cls.manual_review_revision + 1,
                reviewed_at=updated_at,
                reviewed_by=actor,
                review_reason=reason,
                review_decision=decision,
                last_error=(reason if decision == "not_applied" else None),
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def stage_terminal_failure(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            execution_fingerprint: str,
            expected_revision: int,
            history_id: int,
            error: Optional[str],
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以执行指纹和结算版本 CAS 保留失败终态及其历史。"""
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.execution_state == "settling",
                cls.execution_fingerprint == execution_fingerprint,
                cls.settlement_revision == expected_revision,
                cls.lease_token == lease_token,
                cls.lease_expires_at.is_not(None),
                cls.lease_expires_at > now_utc,
            )
            .values(
                execution_state="failed",
                settlement_revision=cls.settlement_revision + 1,
                terminal_history_id=history_id,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
                last_error=error,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def delete_terminal_success(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            execution_fingerprint: str,
            expected_revision: int,
            now_utc: str,
    ) -> int:
        """以执行指纹和结算版本 CAS 删除已成功结算的 pending。"""
        return execute_dml(
            db,
            delete(cls).where(
                cls.task_id == task_id,
                cls.execution_state == "settling",
                cls.execution_fingerprint == execution_fingerprint,
                cls.settlement_revision == expected_revision,
                cls.lease_token == lease_token,
                cls.lease_expires_at.is_not(None),
                cls.lease_expires_at > now_utc,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def record_projection_failure(
            cls,
            db: Session,
            *,
            task_id: str,
            states: tuple[str, ...],
            error: str,
            now_time: str,
            updated_at: str,
    ) -> int:
        """
        在没有有效租约且诊断发生变化时原子记录恢复投影损坏。

        claim 的投影失败会先回滚，因此这里不得重新占用租约。CAS 同时保护
        已被其他 worker 领取的任务，并避免周期恢复反复刷新相同错误。
        :param db: 数据库会话
        :param task_id: 稳定任务标识
        :param states: 可恢复业务状态
        :param error: 可持久化的稳定诊断文本
        :param now_time: 当前 UTC 租约时间
        :param updated_at: 宿主本地业务审计时间
        :return: 更新的记录数，1 表示首次或变化后的诊断被记录
        """
        if not all((task_id, states, error, now_time, updated_at)):
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.state.in_(states),
                or_(
                    cls.lease_token.is_(None),
                    cls.lease_expires_at.is_(None),
                    cls.lease_expires_at <= now_time,
                ),
                cls.last_error.is_distinct_from(error),
            )
            .values(
                last_error=error,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def heartbeat(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            now_time: str,
            lease_expires_at: str,
    ) -> int:
        """
        仅以当前且未过期的 token 原子延长任务租约。

        :param db: 数据库会话
        :param task_id: 稳定任务标识
        :param lease_token: 当前租约令牌
        :param now_time: 当前 UTC 时间
        :param lease_expires_at: 新租约到期时间
        :return: 更新的记录数
        """
        if not all((task_id, lease_token, now_time, lease_expires_at)):
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.lease_token == lease_token,
                cls.lease_expires_at.is_not(None),
                cls.lease_expires_at > now_time,
            )
            .values(
                lease_expires_at=lease_expires_at,
                heartbeat_at=now_time,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def release_claim(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            error: Optional[str],
            now_time: str,
            updated_at: str,
    ) -> int:
        """
        仅以当前且未过期的 token 释放租约并保存本次执行错误。

        :param db: 数据库会话
        :param task_id: 稳定任务标识
        :param lease_token: 当前租约令牌
        :param error: 本次执行错误，成功释放时为空
        :param now_time: 当前 UTC 时间
        :param updated_at: 与既有业务审计字段一致的宿主本地时间
        :return: 更新的记录数
        """
        if not task_id or not lease_token or not now_time or not updated_at:
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.lease_token == lease_token,
                cls.lease_expires_at.is_not(None),
                cls.lease_expires_at > now_time,
            )
            .values(
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
                last_error=error,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def discard_claimed(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            now_time: str,
    ) -> int:
        """
        仅以当前且未过期的 token 删除已经到达终态的租约任务。

        :param db: 数据库会话
        :param task_id: 稳定任务标识
        :param lease_token: 当前租约令牌
        :param now_time: 当前 UTC 时间
        :return: 删除的记录数
        """
        if not task_id or not lease_token or not now_time:
            return 0
        return execute_dml(
            db,
            delete(cls).where(
                cls.task_id == task_id,
                cls.lease_token == lease_token,
                cls.lease_expires_at.is_not(None),
                cls.lease_expires_at > now_time,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def checkpoint_plan(cls, db: Session, *, task_id: str,
                        input_fingerprint: str, checkpoint_version: int,
                        checkpoint_payload: dict[str, Any],
                        source_states: tuple[str, ...], target_state: str,
                        lease_token: str, now_time: str,
                        updated_at: str) -> int:
        """
        以输入指纹为 CAS 条件原子保存计划并推进到已规划。
        :param db: 数据库会话
        :param task_id: 稳定任务标识
        :param input_fingerprint: 规划输入规范 JSON 指纹
        :param checkpoint_version: 检查点格式版本
        :param checkpoint_payload: 完整有序计划 JSON
        :param source_states: 允许推进检查点的起始状态
        :param target_state: 检查点提交后的目标状态
        :param lease_token: 当前且未过期的租约令牌
        :param now_time: 用于租约 fencing 的当前 UTC 时间
        :param updated_at: 与既有业务审计字段一致的宿主本地时间
        :return: 更新的记录数
        """
        if (
                not task_id
                or not input_fingerprint
                or not checkpoint_payload
                or not source_states
                or not target_state
                or not lease_token
                or not updated_at
        ):
            return 0
        values: dict[str, Any] = {
            "state": target_state,
            "checkpoint_version": checkpoint_version,
            "checkpoint_payload": checkpoint_payload,
            "last_error": None,
            "updated_at": updated_at,
        }
        if target_state == "planned":
            values["planned_at"] = updated_at
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.state.in_(source_states),
                cls.input_fingerprint == input_fingerprint,
                cls.lease_token == lease_token,
                cls.lease_expires_at.is_not(None),
                cls.lease_expires_at > now_time,
            )
            .values(**values),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def record_planning_failure(cls, db: Session, *, task_id: str,
                                lease_token: str, error: str,
                                now_time: str, updated_at: str) -> int:
        """
        为接纳态或 provider 待执行任务记录规划失败，不改变其恢复状态。
        :param db: 数据库会话
        :param task_id: 稳定任务标识
        :param lease_token: 当前且未过期的租约令牌
        :param error: 失败原因
        :param now_time: 用于租约 fencing 的当前 UTC 时间
        :param updated_at: 与既有业务审计字段一致的宿主本地时间
        :return: 更新的记录数
        """
        if not task_id or not lease_token or not now_time or not updated_at:
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.state.in_(("accepted", "provider_pending")),
                cls.lease_token == lease_token,
                cls.lease_expires_at.is_not(None),
                cls.lease_expires_at > now_time,
            )
            .values(last_error=error, updated_at=updated_at),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def record_enqueue_failure(cls, db: Session, *, task_id: str,
                               error: str, now_time: str) -> int:
        """
        在调用方会话中记录任务最近一次入队失败。
        :param db: 数据库会话
        :param task_id: 任务标识
        :param error: 失败原因
        :param now_time: 当前时间
        :return: 更新的记录数
        """
        if not task_id:
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.lease_token.is_(None),
            )
            .values(last_error=error, updated_at=now_time),
            execution_options={"synchronize_session": "fetch"},
        )
