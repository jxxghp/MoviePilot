import time
from typing import Any, List, Optional

from sqlalchemy import JSON, Index, Integer, String, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, execute_dml, get_id_column
from app.db.models._constraints import media_identity_constraint
from app.schemas.types import MediaSource


def _title_like(column, title: str):
    """构造跨数据库大小写不敏感的标题匹配条件。"""
    return column.ilike(f"%{title}%")


class DownloadHistory(Base):
    """
    下载历史记录
    """

    id = get_id_column()
    # 保存路径
    path: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # 类型 电影/电视剧/音乐
    type: Mapped[str] = mapped_column(String, nullable=False)
    # 标题
    title: Mapped[str] = mapped_column(String, nullable=False)
    # 年份
    year: Mapped[Optional[str]] = mapped_column(String)
    media_source: Mapped[Optional[str]] = mapped_column(String, index=True)
    media_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 音乐实体类型：recording 单曲、album 专辑
    music_type: Mapped[Optional[str]] = mapped_column(String)
    # Sxx
    seasons: Mapped[Optional[str]] = mapped_column(String)
    # Exx
    episodes: Mapped[Optional[str]] = mapped_column(String)
    # 背景图
    image: Mapped[Optional[str]] = mapped_column(String)
    # 海报
    poster: Mapped[Optional[str]] = mapped_column(String)
    # 下载器
    downloader: Mapped[Optional[str]] = mapped_column(String)
    # 下载任务Hash
    download_hash: Mapped[Optional[str]] = mapped_column(String)
    # 种子名称
    torrent_name: Mapped[Optional[str]] = mapped_column(String)
    # 种子描述
    torrent_description: Mapped[Optional[str]] = mapped_column(String)
    # 种子站点
    torrent_site: Mapped[Optional[str]] = mapped_column(String)
    # 下载用户
    userid: Mapped[Optional[str]] = mapped_column(String)
    # 下载用户名/插件名
    username: Mapped[Optional[str]] = mapped_column(String)
    # 下载渠道
    channel: Mapped[Optional[str]] = mapped_column(String)
    # 创建时间
    date: Mapped[Optional[str]] = mapped_column(String)
    # 附加信息
    note: Mapped[Optional[Any]] = mapped_column(JSON)
    # 实际媒体类别稳定标识
    media_category_id: Mapped[Optional[str]] = mapped_column(String)
    # 实际媒体类别兼容路径快照
    media_category: Mapped[Optional[str]] = mapped_column(String)
    # 命中的分类规则标识
    classification_rule_id: Mapped[Optional[str]] = mapped_column(String)
    # 执行时分类策略版本
    classification_policy_revision: Mapped[Optional[int]] = mapped_column(Integer)
    # 最终分类来源
    classification_source: Mapped[Optional[str]] = mapped_column(String)
    # 剧集组
    episode_group: Mapped[Optional[str]] = mapped_column(String)
    # 自定义识别词（用于整理时应用）
    custom_words: Mapped[Optional[str]] = mapped_column(String)

    __table_args__ = (
        media_identity_constraint("downloadhistory"),
        Index('ix_downloadhistory_download_hash_date', 'download_hash', 'date'),
        Index('ix_downloadhistory_date_id', 'date', 'id'),
        Index('ix_downloadhistory_media_identity', 'media_source', 'media_id'),
    )

    @classmethod
    def get_by_hash(cls, db: Session, download_hash: str):
        return db.execute(
            select(DownloadHistory)
            .where(DownloadHistory.download_hash == download_hash)
            .order_by(DownloadHistory.date.desc())
        ).scalars().first()

    @classmethod
    def get_by_hashes(cls, db: Session, download_hashes: List[str]):
        """
        批量查询多个下载任务的最新历史记录，避免在上层形成 N+1 查询。
        """
        normalized_hashes = []
        seen_hashes = set()
        for download_hash in download_hashes or []:
            if not download_hash or download_hash in seen_hashes:
                continue
            seen_hashes.add(download_hash)
            normalized_hashes.append(download_hash)

        if not normalized_hashes:
            return []

        histories = db.execute(
            select(DownloadHistory)
            .where(DownloadHistory.download_hash.in_(normalized_hashes))
            .order_by(DownloadHistory.download_hash, DownloadHistory.date.desc())
        ).scalars().all()
        latest_histories = {}
        for history in histories:
            if history.download_hash and history.download_hash not in latest_histories:
                latest_histories[history.download_hash] = history

        return [
            latest_histories[download_hash]
            for download_hash in normalized_hashes
            if download_hash in latest_histories
        ]

    @classmethod
    def get_by_media_identity(
            cls, db: Session, media_source: MediaSource, media_id: str,
            music_type: Optional[str] = None,
    ):
        """按规范媒体身份查询下载历史。"""
        if not media_source or media_id is None or not str(media_id).strip():
            return []
        statement = select(DownloadHistory).where(
            DownloadHistory.media_source == str(media_source),
            DownloadHistory.media_id == str(media_id).strip(),
        )
        if music_type:
            statement = statement.where(DownloadHistory.music_type == music_type)
        return list(db.execute(statement).scalars().all())

    @classmethod
    def list_by_page(
        cls, db: Session, page: int = 1, count: int = 30
    ):
        return list(db.execute(
            select(DownloadHistory)
            .order_by(DownloadHistory.date.desc(), DownloadHistory.id.desc())
            .offset((page - 1) * count)
            .limit(count)
        ).scalars().all())

    @classmethod
    async def async_list_by_page(
        cls, db: AsyncSession, page: int = 1, count: int = 30
    ):
        result = await db.execute(
            select(cls)
            .order_by(cls.date.desc(), cls.id.desc())
            .offset((page - 1) * count)
            .limit(count)
        )
        return list(result.scalars().all())

    @classmethod
    async def async_list_by_title(
        cls,
        db: AsyncSession,
        title: str,
        page: int = 1,
        count: int = 30,
    ):
        query = (
            select(cls).filter(_title_like(cls.title, title)).order_by(cls.date.desc())
        )
        query = query.offset((page - 1) * count).limit(count)
        result = await db.execute(query)
        return list(result.scalars().all())

    @classmethod
    async def async_count(cls, db: AsyncSession):
        result = await db.execute(select(func.count(cls.id)))
        return result.scalar()

    @classmethod
    async def async_count_by_title(cls, db: AsyncSession, title: str):
        result = await db.execute(
            select(func.count(cls.id)).filter(_title_like(cls.title, title))
        )
        return result.scalar()

    @classmethod
    def get_by_path(cls, db: Session, path: str):
        return db.execute(
            select(DownloadHistory).where(DownloadHistory.path == path)
        ).scalars().first()

    @classmethod
    def get_last_by(
        cls,
        db: Session,
        mtype: Optional[str] = None,
        title: Optional[str] = None,
        year: Optional[str] = None,
        season: Optional[str] = None,
        episode: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
    ):
        """
        按媒体身份、季集或标题年份查询下载记录。
        """
        if media_source and media_id and mtype:
            statement = select(DownloadHistory).where(
                DownloadHistory.media_source == str(media_source),
                DownloadHistory.media_id == str(media_id),
                DownloadHistory.type == mtype,
            )
        elif title and year:
            statement = select(DownloadHistory).where(
                DownloadHistory.title == title,
                DownloadHistory.year == year,
            )
        else:
            return []
        # 季、集逐级收窄：给出季才可能给集，与原六条分支等价
        if season is not None:
            statement = statement.where(DownloadHistory.seasons == season)
            if episode:
                statement = statement.where(DownloadHistory.episodes == episode)
        return list(db.execute(
            statement.order_by(DownloadHistory.id.desc())
        ).scalars().all())


    @classmethod
    def list_by_user_date(cls, db: Session, date: str, username: Optional[str] = None):
        """
        查询某用户某时间之前的下载历史。

        条件是 date < 传入时刻，等于该时刻的那条不计入；oper 层的同名方法描述一致。
        :param db: 数据库会话
        :param date: 时间水位，取该时刻之前的记录
        :param username: 下载用户，不传则跨用户返回
        :return: 下载历史列表，按主键倒序
        """
        statement = select(DownloadHistory).where(DownloadHistory.date < date)
        if username:
            statement = statement.where(DownloadHistory.username == username)
        return list(db.execute(
            statement.order_by(DownloadHistory.id.desc())
        ).scalars().all())

    @classmethod
    def list_by_date(
        cls,
        db: Session,
        date: str,
        type: str,
        media_source: MediaSource,
        media_id: str,
        seasons: Optional[str] = None,
    ):
        """
        查询某时间之后的下载历史
        """
        statement = select(DownloadHistory).where(
            DownloadHistory.date > date,
            DownloadHistory.type == type,
            DownloadHistory.media_source == str(media_source),
            DownloadHistory.media_id == str(media_id),
        )
        if seasons:
            statement = statement.where(DownloadHistory.seasons == seasons)
        return list(db.execute(
            statement.order_by(DownloadHistory.id.desc())
        ).scalars().all())

    @classmethod
    def list_by_type(cls, db: Session, mtype: str, days: int):
        return list(db.execute(
            select(DownloadHistory).where(
                DownloadHistory.type == mtype,
                DownloadHistory.date
                >= time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 86400 * int(days))
                ),
            )
        ).scalars().all())

    @classmethod
    def delete_before(
        cls,
        db: Session,
        before_time: str,
        limit: Optional[int] = 500,
    ) -> int:
        """
        分批删除指定时间之前的下载历史。
        """
        ids = db.execute(
            select(cls.id)
            .where(cls.date < before_time)
            .order_by(cls.id.asc())
            .limit(limit)
        ).scalars().all()
        if not ids:
            return 0
        return execute_dml(
            db, delete(cls).where(cls.id.in_(ids)),
            execution_options={"synchronize_session": False},
        )


class DownloadFiles(Base):
    """
    下载文件记录
    """

    id = get_id_column()
    # 下载器
    downloader: Mapped[Optional[str]] = mapped_column(String)
    # 下载任务Hash
    download_hash: Mapped[Optional[str]] = mapped_column(String)
    # 完整路径
    fullpath: Mapped[Optional[str]] = mapped_column(String)
    # 保存路径
    savepath: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 文件相对路径/名称
    filepath: Mapped[Optional[str]] = mapped_column(String)
    # 种子名称
    torrentname: Mapped[Optional[str]] = mapped_column(String)
    # 状态 0-已删除 1-正常
    state: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        Index('ix_downloadfiles_download_hash_state', 'download_hash', 'state'),
        Index('ix_downloadfiles_fullpath_id', 'fullpath', 'id'),
    )

    @classmethod
    def get_by_hash(cls, db: Session, download_hash: str, state: Optional[int] = None):
        statement = select(cls).where(cls.download_hash == download_hash)
        if state is not None:
            statement = statement.where(cls.state == state)
        return list(db.execute(statement).scalars().all())

    @classmethod
    def get_by_fullpath(cls, db: Session, fullpath: str, all_files: bool = False):
        result = db.execute(
            select(cls).where(cls.fullpath == fullpath).order_by(cls.id.desc())
        ).scalars()
        return list(result.all()) if all_files else result.first()

    @classmethod
    def get_by_savepath(cls, db: Session, savepath: str):
        return list(db.execute(select(cls).where(cls.savepath == savepath)).scalars().all())

    @classmethod
    def delete_by_fullpath(cls, db: Session, fullpath: str):
        db.execute(
            update(cls).where(cls.fullpath == fullpath, cls.state == 1).values(state=0)
        )

    @classmethod
    def delete_orphans(
        cls,
        db: Session,
        limit: Optional[int] = 500,
    ) -> int:
        """
        分批删除已找不到父下载历史的文件记录。

        downloadfiles 没有时间字段，无法安全地按时间直接裁剪，
        因此只清理明确失去父记录的孤儿数据。
        """
        ids = db.execute(
            select(cls.id)
            .outerjoin(
                DownloadHistory,
                DownloadHistory.download_hash == cls.download_hash,
            )
            .where(DownloadHistory.id.is_(None))
            .order_by(cls.id.asc())
            .limit(limit)
        ).scalars().all()
        if not ids:
            return 0
        return execute_dml(
            db, delete(cls).where(cls.id.in_(ids)),
            execution_options={"synchronize_session": False},
        )
