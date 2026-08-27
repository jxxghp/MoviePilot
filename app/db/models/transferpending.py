import hashlib
import json
from datetime import datetime
from typing import Any, List, Optional, cast
from uuid import uuid4

from sqlalchemy import JSON, Index, Integer, String, Text, UniqueConstraint, delete, select, update
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
    旧路径登记接口仍生成最小 legacy_replan 输入，供插件兼容调用方继续使用。
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
        UniqueConstraint("task_id", name="uq_transferpending_task_id"),
    )

    @classmethod
    def register(cls, db: Session, storage: str, src_path: str,
                 now_time: str) -> Optional["TransferPending"]:
        """
        登记一个待整理文件，已存在时保持原登记时间不变。
        :param db: 数据库会话
        :param storage: 存储
        :param src_path: 源文件路径
        :param now_time: 当前时间
        :return: 登记记录
        """
        if not storage or not src_path:
            return None
        pending = db.execute(
            select(cls).where(cls.storage == storage, cls.src_path == src_path)
        ).scalars().first()
        if pending:
            return cast("TransferPending", pending)
        planning_input = _legacy_planning_payload(storage, src_path)
        pending = cls(
            storage=storage,
            src_path=src_path,
            state="accepted",
            created_at=now_time,
            updated_at=now_time,
            input_version=1,
            planning_input=planning_input,
            input_fingerprint=_planning_fingerprint(planning_input),
        )
        db.add(pending)
        return pending

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
    def list_by_state(cls, db: Session, *, state: str,
                      limit: Optional[int] = 5000) -> List["TransferPending"]:
        """
        按登记顺序列出指定持久状态的接纳记录。
        :param db: 数据库会话
        :param state: 持久状态
        :param limit: 单次读取上限
        :return: 接纳记录列表
        """
        if not state:
            return []
        return list(db.execute(
            select(cls)
            .where(cls.state == state)
            .order_by(cls.created_at.asc(), cls.id.asc())
            .limit(limit)
        ).scalars().all())

    @classmethod
    def list_by_states(cls, db: Session, *, states: tuple[str, ...],
                       limit: Optional[int] = 5000) -> List["TransferPending"]:
        """
        按登记顺序列出多个可恢复持久状态的记录。
        :param db: 数据库会话
        :param states: 允许恢复的状态集合
        :param limit: 单次读取上限
        :return: 接纳记录列表
        """
        if not states:
            return []
        return list(db.execute(
            select(cls)
            .where(cls.state.in_(states))
            .order_by(cls.created_at.asc(), cls.id.asc())
            .limit(limit)
        ).scalars().all())

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
    def checkpoint_plan(cls, db: Session, *, task_id: str,
                        input_fingerprint: str, checkpoint_version: int,
                        checkpoint_payload: dict[str, Any],
                        source_states: tuple[str, ...], target_state: str,
                        now_time: str) -> int:
        """
        以输入指纹为 CAS 条件原子保存计划并推进到已规划。
        :param db: 数据库会话
        :param task_id: 稳定任务标识
        :param input_fingerprint: 规划输入规范 JSON 指纹
        :param checkpoint_version: 检查点格式版本
        :param checkpoint_payload: 完整有序计划 JSON
        :param source_states: 允许推进检查点的起始状态
        :param target_state: 检查点提交后的目标状态
        :param now_time: 当前时间
        :return: 更新的记录数
        """
        if (
                not task_id
                or not input_fingerprint
                or not checkpoint_payload
                or not source_states
                or not target_state
        ):
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.state.in_(source_states),
                cls.input_fingerprint == input_fingerprint,
            )
            .values(
                state=target_state,
                checkpoint_version=checkpoint_version,
                checkpoint_payload=checkpoint_payload,
                planned_at=now_time,
                last_error=None,
                updated_at=now_time,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def record_planning_failure(cls, db: Session, *, task_id: str,
                                error: str, now_time: str) -> int:
        """
        为接纳态或 provider 待执行任务记录规划失败，不改变其恢复状态。
        :param db: 数据库会话
        :param task_id: 稳定任务标识
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
                cls.state.in_(("accepted", "provider_pending")),
            )
            .values(last_error=error, updated_at=now_time),
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
            .where(cls.task_id == task_id)
            .values(last_error=error, updated_at=now_time),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def discard_task(cls, db: Session, *, task_id: str) -> int:
        """
        在调用方会话中按任务标识删除接纳记录。
        :param db: 数据库会话
        :param task_id: 任务标识
        :return: 删除的记录数
        """
        if not task_id:
            return 0
        return execute_dml(
            db, delete(cls).where(cls.task_id == task_id),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def discard(cls, db: Session, storage: str, src_path: str) -> int:
        """
        注销一个待整理文件登记，整理到达终态（成功或失败）时调用。
        :param db: 数据库会话
        :param storage: 存储
        :param src_path: 源文件路径
        :return: 删除的记录数
        """
        if not storage or not src_path:
            return 0
        return execute_dml(
            db, delete(cls).where(cls.storage == storage, cls.src_path == src_path),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def list_all(cls, db: Session, limit: Optional[int] = 5000) -> List["TransferPending"]:
        """
        列出全部待整理登记，供启动回放使用。

        按登记时间升序回放，保持与原入队顺序一致；上限避免异常积压时
        一次性把整理链压垮。
        :param db: 数据库会话
        :param limit: 单次回放上限
        :return: 待整理登记列表
        """
        return list(db.execute(
            select(cls)
            .order_by(cls.created_at.asc(), cls.id.asc())
            .limit(limit)
        ).scalars().all())

    @classmethod
    def clear(cls, db: Session) -> int:
        """
        清空全部待整理登记。
        :param db: 数据库会话
        :return: 删除的记录数
        """
        return execute_dml(
            db, delete(cls),
            execution_options={"synchronize_session": False},
        )
