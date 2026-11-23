"""
已受理但尚未在下载器中确认的下载提交 ORM 模型与会话内数据操作。

会话及事务由调用方管理，本模块不负责提交或回滚。
"""

from typing import Any, List, Optional, Self

from sqlalchemy import JSON, Index, Integer, String, delete, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, execute_dml, get_id_column
from app.db.models._constraints import media_identity_constraint


class DownloadPending(Base):
    """
    下载器已受理添加但任务信息尚未确认的待核对提交记录。
    """

    id = get_id_column()
    # 单条待确认提交的稳定唯一键，保证并发幂等写入和精确删除
    pending_key: Mapped[str] = mapped_column(String, nullable=False)
    # 媒体级目标键，用于按电影、音乐或电视剧作品批量查询活跃待确认记录
    target_key: Mapped[str] = mapped_column(String, nullable=False)
    # 媒体类型（电影/电视剧/音乐）
    type: Mapped[Optional[str]] = mapped_column(String)
    # 识别出的标准媒体标题
    title: Mapped[Optional[str]] = mapped_column(String)
    # 媒体发行年份
    year: Mapped[Optional[str]] = mapped_column(String)
    # 标准媒体数据源与原生 ID
    media_source: Mapped[Optional[str]] = mapped_column(String)
    media_id: Mapped[Optional[str]] = mapped_column(String)
    # 电视剧单季季号；电影、音乐或多季整包为 None
    season: Mapped[Optional[int]] = mapped_column(Integer)
    # 目标集号列表；整季或非剧集为 None
    episodes: Mapped[Optional[List[int]]] = mapped_column(JSON)
    # 受理或请求指向的下载器名称；空字符串表示默认下载器
    downloader: Mapped[Optional[str]] = mapped_column(String)
    # 种子解析出的已知小写 v1/v2 哈希列表
    hashes: Mapped[Optional[List[str]]] = mapped_column(JSON)
    # 种子原始标题
    torrent_name: Mapped[Optional[str]] = mapped_column(String)
    # 触发本次提交的业务来源
    source: Mapped[Optional[str]] = mapped_column(String)
    # 关联订阅主键；非订阅来源为 None
    subscription_id: Mapped[Optional[int]] = mapped_column(Integer)
    # 核对转正结算所需的脱敏快照（不含 Cookie、Passkey 或签名下载链接）
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    # 首次进入待确认状态的时间文本
    submitted_at: Mapped[str] = mapped_column(String, nullable=False)
    # 最近一次核对下载器的时间文本
    last_checked_at: Mapped[Optional[str]] = mapped_column(String)
    # 保护窗口截止时间文本
    expires_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        media_identity_constraint("downloadpending"),
        Index("ux_downloadpending_pending_key", "pending_key", unique=True),
        Index("ix_downloadpending_target_key", "target_key"),
        Index("ix_downloadpending_subscription_id", "subscription_id"),
        Index("ix_downloadpending_expires_at", "expires_at"),
    )

    @classmethod
    def list_all(cls, db: Session) -> List["DownloadPending"]:
        """
        查询当前全部待确认下载记录，按主键顺序返回。

        :param db: 调用方持有的同步数据库会话
        :return: 待确认记录列表
        """
        return list(db.execute(select(cls).order_by(cls.id.asc())).scalars().all())

    @classmethod
    def list_by_target_keys(
        cls,
        db: Session,
        target_keys: List[str],
    ) -> List["DownloadPending"]:
        """
        按媒体级目标键批量查询待确认下载记录。

        :param db: 调用方持有的同步数据库会话
        :param target_keys: 待查询的目标键列表
        :return: 匹配的待确认记录列表
        """
        normalized = list(dict.fromkeys([key for key in target_keys if key]))
        if not normalized:
            return []
        return list(
            db.execute(
                select(cls)
                .where(cls.target_key.in_(normalized))
                .order_by(cls.id.asc())
            ).scalars().all()
        )

    @classmethod
    def list_by_subscription_id(
        cls,
        db: Session,
        subscription_id: int,
    ) -> List["DownloadPending"]:
        """
        按订阅主键查询归属该订阅的待确认记录。

        :param db: 调用方持有的同步数据库会话
        :param subscription_id: 订阅主键
        :return: 匹配的待确认记录列表
        """
        return list(
            db.execute(
                select(cls)
                .where(cls.subscription_id == subscription_id)
                .order_by(cls.id.asc())
            ).scalars().all()
        )

    @classmethod
    def get_by_key(cls, db: Session, pending_key: str) -> Optional["DownloadPending"]:
        """
        按唯一键查询单条待确认记录。

        :param db: 调用方持有的同步数据库会话
        :param pending_key: 待确认记录唯一键
        :return: 匹配记录；不存在时返回 None
        """
        if not pending_key:
            return None
        record: Optional[DownloadPending] = db.execute(
            select(cls).where(cls.pending_key == pending_key)
        ).scalars().first()
        return record

    @classmethod
    def upsert(
        cls,
        db: Session,
        pending_key: str,
        **kwargs: object,
    ) -> "DownloadPending":
        """
        按唯一键新增或更新一条待确认记录。

        :param db: 调用方持有的同步数据库会话
        :param pending_key: 待确认记录唯一键
        :param kwargs: 需写入的模型列值
        :return: 暂存在会话中的 ORM 实例
        """
        existing: Optional[Self] = db.execute(
            select(cls).where(cls.pending_key == pending_key)
        ).scalars().first()
        if existing is not None:
            for key, value in kwargs.items():
                setattr(existing, key, value)
            return existing
        record = cls(pending_key=pending_key, **kwargs)
        db.add(record)
        return record

    @classmethod
    def touch_checked(
        cls,
        db: Session,
        pending_key: str,
        checked_at: str,
    ) -> Optional["DownloadPending"]:
        """
        更新单条待确认记录的最近核对时间。

        :param db: 调用方持有的同步数据库会话
        :param pending_key: 待确认记录唯一键
        :param checked_at: 本次核对时间文本
        :return: 更新后的 ORM 实例；记录不存在时返回 None
        """
        record = cls.get_by_key(db, pending_key)
        if record is None:
            return None
        record.last_checked_at = checked_at
        return record

    @classmethod
    def delete_by_key(cls, db: Session, pending_key: str) -> int:
        """
        按唯一键删除待确认记录。

        :param db: 调用方持有的同步数据库会话
        :param pending_key: 待确认记录唯一键
        :return: 删除的行数
        """
        if not pending_key:
            return 0
        return execute_dml(
            db,
            delete(cls).where(cls.pending_key == pending_key),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def delete_by_target_key(cls, db: Session, target_key: str) -> int:
        """
        按媒体级目标键删除关联的待确认记录。

        :param db: 调用方持有的同步数据库会话
        :param target_key: 媒体级目标键
        :return: 删除的行数
        """
        if not target_key:
            return 0
        return execute_dml(
            db,
            delete(cls).where(cls.target_key == target_key),
            execution_options={"synchronize_session": False},
        )
