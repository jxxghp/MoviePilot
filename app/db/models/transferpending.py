from typing import List, Optional

from sqlalchemy import Index, String, delete, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, execute_dml, get_id_column


class TransferPending(Base):
    """
    待整理文件登记。

    整理队列是纯内存的 queue.Queue：进程一旦重启（挂载挂死后的人工重启、版本
    升级、OOM、宿主重启），队列里的任务会连同「这些文件还没整理」这个事实一起
    蒸发。而已经稳定落地的文件不会再产生任何监控事件，也不会有新的补偿扫描起点
    ——结果就是永久漏件，只能靠人工比对补整理。

    这里只落盘最小事实：存储与源文件路径。重启后重新走一遍整理入口，由整理历史
    查重挡掉已经完成的，因此不需要序列化 meta/mediainfo 这些重对象，也不存在
    识别结果陈旧的问题。
    """

    id = get_id_column()
    # 存储
    storage: Mapped[str] = mapped_column(String, nullable=False)
    # 源文件路径
    src_path: Mapped[str] = mapped_column(String, nullable=False)
    # 登记时间
    created_at: Mapped[Optional[str]] = mapped_column(String)

    __table_args__ = (
        # 同一个文件重复入队只保留一条，回放时不会重复送入整理链
        Index("ux_transferpending_storage_path", "storage", "src_path", unique=True),
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
            return pending
        pending = cls(storage=storage, src_path=src_path, created_at=now_time)
        db.add(pending)
        return pending

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
