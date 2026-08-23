import re
import time
from pathlib import Path
from typing import Any, List, Optional

from sqlalchemy import Boolean, Index, Integer, JSON, String, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, execute_dml, get_id_column
from app.db.decorators import (
    legacy_async_db_query,
    legacy_db_query,
)
from app.db.models._constraints import media_identity_constraint
from app.schemas.types import MUSIC_ENTITY_ALBUM, MUSIC_ENTITY_RECORDING, MediaSource, MediaType


def _text_like(column, pattern: str, wildcard: bool = False):
    """构造跨数据库大小写不敏感的文本匹配条件。"""
    if wildcard:
        return column.ilike(pattern, escape='\\')
    return column.ilike(pattern)


class TransferHistory(Base):
    """
    整理记录
    """
    id = get_id_column()
    # 源路径
    src: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 源存储
    src_storage: Mapped[str] = mapped_column(String, nullable=False, default="local")
    # 源文件项
    src_fileitem: Mapped[Optional[Any]] = mapped_column(JSON, default=dict)
    # 目标路径
    dest: Mapped[Optional[str]] = mapped_column(String)
    # 目标存储
    dest_storage: Mapped[Optional[str]] = mapped_column(String)
    # 目标文件项
    dest_fileitem: Mapped[Optional[Any]] = mapped_column(JSON, default=dict)
    # 转移模式 move/copy/link...
    mode: Mapped[Optional[str]] = mapped_column(String)
    # 类型 电影/电视剧
    type: Mapped[Optional[str]] = mapped_column(String)
    # 二级分类
    category: Mapped[Optional[str]] = mapped_column(String)
    # 标题
    title: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 年份
    year: Mapped[Optional[str]] = mapped_column(String)
    # 媒体数据源与原生ID
    media_source: Mapped[Optional[str]] = mapped_column(String, index=True)
    media_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 音乐实体类型：recording 单曲、album 专辑
    music_type: Mapped[Optional[str]] = mapped_column(String)
    # 专辑预期总曲目数
    total_tracks: Mapped[Optional[int]] = mapped_column(Integer)
    # 实际音频格式
    audio_format: Mapped[Optional[str]] = mapped_column(String)
    # 是否无损音频
    audio_lossless: Mapped[Optional[bool]] = mapped_column(Boolean)
    # 实际位深（bit）
    bit_depth: Mapped[Optional[int]] = mapped_column(Integer)
    # 实际采样率（Hz）
    sample_rate: Mapped[Optional[int]] = mapped_column(Integer)
    # 实际码率（bps）
    bitrate: Mapped[Optional[int]] = mapped_column(Integer)
    # Sxx
    seasons: Mapped[Optional[str]] = mapped_column(String)
    # Exx
    episodes: Mapped[Optional[str]] = mapped_column(String)
    # 海报
    image: Mapped[Optional[str]] = mapped_column(String)
    # 下载器
    downloader: Mapped[Optional[str]] = mapped_column(String)
    # 下载器hash
    download_hash: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 转移成功状态
    status: Mapped[Optional[bool]] = mapped_column(Boolean(), default=True)
    # 转移失败信息
    errmsg: Mapped[Optional[str]] = mapped_column(String)
    # 时间
    date: Mapped[Optional[str]] = mapped_column(String)
    # 文件清单，以JSON存储
    files: Mapped[Optional[Any]] = mapped_column(JSON, default=list)
    # 剧集组
    episode_group: Mapped[Optional[str]] = mapped_column(String)

    __table_args__ = (
        media_identity_constraint("transferhistory"),
        Index('ix_transferhistory_status_date', 'status', 'date'),
        Index('ix_transferhistory_date_id', 'date', 'id'),
        Index('ix_transferhistory_media_identity', 'media_source', 'media_id'),
        Index('ux_transferhistory_src_storage', 'src', 'src_storage', unique=True),
    )

    @classmethod
    @legacy_db_query
    def list_by_title(cls, db: Session, title: str, page: int = 1, count: int = 30,
                      status: Optional[bool] = None, wildcard: bool = False):
        if wildcard:
            text_filter = or_(
                _text_like(cls.title, title, wildcard=True),
                _text_like(cls.src, title, wildcard=True),
                _text_like(cls.dest, title, wildcard=True),
            )
        else:
            text_filter = or_(
                _text_like(cls.title, f'%{title}%'),
                _text_like(cls.src, f'%{title}%'),
                _text_like(cls.dest, f'%{title}%'),
            )
        statement = select(cls).where(text_filter)
        if status is not None:
            statement = statement.where(cls.status == status)
        statement = statement.order_by(cls.date.desc())

        # 当count为负数时，不限制页数查询所有
        if count >= 0:
            statement = statement.offset((page - 1) * count).limit(count)

        return list(db.execute(statement).scalars().all())

    @classmethod
    @legacy_async_db_query
    async def async_list_by_title(cls, db: AsyncSession, title: str, page: int = 1, count: int = 30,
                                  status: Optional[bool] = None, wildcard: bool = False):
        if wildcard:
            text_filter = or_(
                _text_like(cls.title, title, wildcard=True),
                _text_like(cls.src, title, wildcard=True),
                _text_like(cls.dest, title, wildcard=True),
            )
        else:
            text_filter = or_(
                _text_like(cls.title, f'%{title}%'),
                _text_like(cls.src, f'%{title}%'),
                _text_like(cls.dest, f'%{title}%'),
            )
        query = select(cls).filter(text_filter)
        if status is not None:
            query = query.filter(cls.status == status)
        query = query.order_by(cls.date.desc())

        # 当count为负数时，不限制页数查询所有
        if count >= 0:
            query = query.offset((page - 1) * count).limit(count)

        result = await db.execute(query)
        return list(result.scalars().all())

    @classmethod
    @legacy_db_query
    def list_by_page(cls, db: Session, page: int = 1, count: int = 30, status: Optional[bool] = None):
        statement = select(cls)
        if status is not None:
            statement = statement.where(cls.status == status)
        statement = statement.order_by(cls.date.desc())

        # 当count为负数时，不限制页数查询所有
        if count >= 0:
            statement = statement.offset((page - 1) * count).limit(count)

        return list(db.execute(statement).scalars().all())

    @classmethod
    @legacy_async_db_query
    async def async_list_by_page(cls, db: AsyncSession, page: int = 1, count: int = 30,
                                 status: Optional[bool] = None):
        if status is not None:
            query = select(cls).filter(
                cls.status == status
            ).order_by(
                cls.date.desc()
            )
        else:
            query = select(cls).order_by(
                cls.date.desc()
            )
        
        # 当count为负数时，不限制页数查询所有
        if count >= 0:
            query = query.offset((page - 1) * count).limit(count)
        
        result = await db.execute(query)
        return list(result.scalars().all())

    @classmethod
    @legacy_db_query
    def get_by_hash(
        cls,
        db: Session | str | None = None,
        download_hash: str | None = None,
    ):
        """按下载哈希查询最新记录，兼容旧插件无会话调用。"""
        if download_hash is None and isinstance(db, str):
            download_hash, db = db, None
        if download_hash is None:
            raise TypeError("download_hash is required")

        def query(session: Session):
            """在调用方提供的同步会话中执行哈希查询。"""
            return session.execute(
                select(cls).where(cls.download_hash == download_hash)
            ).scalars().first()

        return query(db)

    @classmethod
    @legacy_db_query
    def get_by_src(
            cls, db: Session | str | None = None, src: str | None = None,
            storage: Optional[str] = None
    ) -> Optional["TransferHistory"]:
        """
        按源路径和存储查询单条整理记录。

        :param db: 数据库会话
        :param src: 源路径
        :param storage: 源存储类型
        :return: 命中的整理记录，未命中时返回 None
        """
        if src is None and isinstance(db, str):
            src, db = db, None
        if src is None:
            raise TypeError("src is required")

        def query(session: Session):
            """在调用方提供的同步会话中执行源路径查询。"""
            statement = select(cls).where(cls.src == src)
            if storage:
                statement = statement.where(cls.src_storage == storage)
            return session.execute(
                statement.order_by(cls.id.desc())
            ).scalars().first()

        return query(db)

    @classmethod
    @legacy_db_query
    def get_success_by_src(
            cls, db: Session | str | None = None, src: str | None = None,
            storage: Optional[str] = None
    ) -> Optional["TransferHistory"]:
        """
        按源路径和存储查询成功的整理记录，源路径原样精确匹配。

        与 list_success_by_src 不同，这里不对源路径做归一化，蓝光原盘目录记录
        带尾斜杠，归一化后反而匹配不到。
        :param db: 数据库会话
        :param src: 源路径
        :param storage: 源存储类型
        :return: 命中的成功整理记录，未命中时返回 None
        """
        if src is None and isinstance(db, str):
            src, db = db, None
        if src is None:
            raise TypeError("src is required")

        def query(session: Session):
            """在调用方提供的同步会话中执行成功源路径查询。"""
            statement = select(cls).where(cls.src == src, cls.status.is_(True))
            if storage:
                statement = statement.where(cls.src_storage == storage)
            return session.execute(
                statement.order_by(cls.id.desc())
            ).scalars().first()

        return query(db)

    @classmethod
    @legacy_db_query
    def get_by_dest(
            cls, db: Session | str | None = None, dest: str | None = None,
            storage: Optional[str] = None
    ) -> Optional["TransferHistory"]:
        """
        按目标路径和存储查询单条整理记录。

        :param db: 数据库会话
        :param dest: 目标路径
        :param storage: 目标存储类型
        :return: 命中的整理记录，未命中时返回 None
        """
        if dest is None and isinstance(db, str):
            dest, db = db, None
        if dest is None:
            raise TypeError("dest is required")

        def query(session: Session):
            """在调用方提供的同步会话中执行目标路径查询。"""
            statement = select(cls).where(cls.dest == dest)
            if storage:
                statement = statement.where(cls.dest_storage == storage)
            return session.execute(
                statement.order_by(cls.id.desc())
            ).scalars().first()

        return query(db)

    @classmethod
    @legacy_db_query
    def list_success_by_src(
            cls,
            db: Session,
            src: str,
            storage: Optional[str] = None,
            recursive: bool = False,
    ) -> List["TransferHistory"]:
        """
        按源路径查询成功整理记录，目录模式仅匹配其直接或间接子项。

        :param db: 数据库会话
        :param src: 源路径
        :param storage: 源存储类型
        :param recursive: 是否递归匹配目录子项
        :return: 命中的成功整理记录
        """
        normalized_src = (
            Path(str(src).replace("\\", "/")).as_posix().rstrip("/") or "/"
        )
        statement = select(cls).where(cls.status.is_(True))
        if recursive:
            escaped_src = (
                normalized_src.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            statement = statement.where(
                or_(
                    cls.src == normalized_src,
                    cls.src.like(f"{escaped_src.rstrip('/')}/%", escape="\\"),
                )
            )
        else:
            statement = statement.where(cls.src == normalized_src)
        if storage:
            statement = statement.where(cls.src_storage == storage)
        return list(db.execute(statement).scalars().all())

    @classmethod
    @legacy_db_query
    def list_success_move_by_dest(
            cls,
            db: Session,
            dest: str,
            storage: Optional[str] = None,
            recursive: bool = False,
    ) -> List["TransferHistory"]:
        """
        按目标路径查询成功移动记录，供从媒体库现址发起重新整理时识别历史。

        :param db: 数据库会话
        :param dest: 目标路径
        :param storage: 目标存储类型
        :param recursive: 是否递归匹配目录子项
        :return: 命中的成功移动记录
        """
        normalized_dest = (
            Path(str(dest).replace("\\", "/")).as_posix().rstrip("/") or "/"
        )
        statement = select(cls).where(
            cls.status.is_(True),
            cls.mode.contains("move"),
        )
        if recursive:
            escaped_dest = (
                normalized_dest.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            statement = statement.where(
                or_(
                    cls.dest == normalized_dest,
                    cls.dest.like(f"{escaped_dest.rstrip('/')}/%", escape="\\"),
                )
            )
        else:
            statement = statement.where(cls.dest == normalized_dest)
        if storage:
            statement = statement.where(cls.dest_storage == storage)
        return list(db.execute(statement).scalars().all())

    @classmethod
    @legacy_db_query
    def list_by_hash(cls, db: Session, download_hash: str):
        return list(db.execute(
            select(cls).where(cls.download_hash == download_hash)
        ).scalars().all())

    @classmethod
    @legacy_db_query
    def statistic(cls, db: Session, days: int = 7):
        """
        统计最近days天的下载历史数量，按日期分组返回每日数量
        """
        sub_query = select(
            func.substr(cls.date, 1, 10).label('date'),
            cls.id.label('id')
        ).where(
            cls.date >= time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(time.time() - 86400 * days))
        ).subquery()
        return list(db.execute(
            select(sub_query.c.date, func.count(sub_query.c.id)).group_by(sub_query.c.date)
        ).all())

    @classmethod
    @legacy_db_query
    def monthly_media_statistics(cls, db: Session):
        """
        统计当月成功整理的电影、电视剧、剧集和音乐数量。

        电影和电视剧按媒体身份去重；剧集优先按历史记录中的集数字段计算，
        缺少集数时按单条成功整理记录计数；音乐按曲目身份去重，整专记录不能只按专辑 ID 合并。
        """
        month_prefix = time.strftime("%Y-%m-", time.localtime())
        histories = db.execute(select(cls).where(
            cls.status.is_(True),
            cls.date.like(f"{month_prefix}%"),
            cls.type.in_([MediaType.MOVIE.value, MediaType.TV.value, MediaType.MUSIC.value]),
        )).scalars().all()
        movie_identities = set()
        tv_identities = set()
        episode_count = 0
        music_identities = set()

        for history in histories:
            if history.type == MediaType.MUSIC.value:
                music_identities.add(cls._music_history_identity(history))
                continue

            identity = (
                history.media_source or "",
                history.media_id or "",
                history.title or "",
                history.year or "",
            )
            if history.type == MediaType.MOVIE.value:
                movie_identities.add(identity)
                continue

            tv_identities.add(identity)
            episode_count += cls._history_episode_count(history)

        return len(movie_identities), len(tv_identities), episode_count, len(music_identities)

    @staticmethod
    def _music_history_identity(history: "TransferHistory") -> tuple:
        """构造曲目级历史身份，避免整专内全部曲目被同一专辑 ID 合并。"""
        source = str(history.media_source or "").strip().casefold()
        media_id = str(history.media_id or "").strip()
        music_type = str(history.music_type or MUSIC_ENTITY_RECORDING).strip().casefold()
        path_identity = str(history.dest or history.src or "").replace("\\", "/").casefold()
        if music_type == MUSIC_ENTITY_ALBUM:
            return source, media_id, music_type, path_identity or history.title or history.id
        if media_id:
            return source, media_id, music_type
        return source, music_type, path_identity or history.title or history.id

    @staticmethod
    def _history_episode_count(history: "TransferHistory") -> int:
        """从单条整理历史中估算成功入库的剧集数量。"""
        episode_numbers = [int(value) for value in re.findall(r"\d+", history.episodes or "")]
        if len(episode_numbers) >= 2 and "-" in (history.episodes or ""):
            return max(1, episode_numbers[-1] - episode_numbers[0] + 1)
        if episode_numbers:
            return len(set(episode_numbers))
        if isinstance(history.files, list) and history.files:
            return len(history.files)

        return 1

    @classmethod
    @legacy_async_db_query
    async def async_statistic(cls, db: AsyncSession, days: int = 7):
        """
        统计最近days天的下载历史数量，按日期分组返回每日数量
        """
        sub_query = select(func.substr(cls.date, 1, 10).label('date'),
                           cls.id.label('id')).filter(
            cls.date >= time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(time.time() - 86400 * days))).subquery()
        result = await db.execute(
            select(sub_query.c.date, func.count(sub_query.c.id)).group_by(sub_query.c.date)
        )
        return result.all()

    @classmethod
    @legacy_db_query
    def count(cls, db: Session, status: Optional[bool] = None):
        statement = select(func.count(cls.id))
        if status is not None:
            statement = statement.where(cls.status == status)
        return db.execute(statement).scalar()

    @classmethod
    @legacy_async_db_query
    async def async_count(cls, db: AsyncSession, status: Optional[bool] = None):
        if status is not None:
            result = await db.execute(
                select(func.count(cls.id)).filter(cls.status == status)
            )
        else:
            result = await db.execute(
                select(func.count(cls.id))
            )
        return result.scalar()

    @classmethod
    @legacy_db_query
    def count_by_title(cls, db: Session, title: str, status: Optional[bool] = None, wildcard: bool = False):
        if wildcard:
            text_filter = or_(
                _text_like(cls.title, title, wildcard=True),
                _text_like(cls.src, title, wildcard=True),
                _text_like(cls.dest, title, wildcard=True),
            )
        else:
            text_filter = or_(
                _text_like(cls.title, f'%{title}%'),
                _text_like(cls.src, f'%{title}%'),
                _text_like(cls.dest, f'%{title}%'),
            )
        statement = select(func.count(cls.id)).where(text_filter)
        if status is not None:
            statement = statement.where(cls.status == status)
        return db.execute(statement).scalar()

    @classmethod
    @legacy_async_db_query
    async def async_count_by_title(cls, db: AsyncSession, title: str, status: Optional[bool] = None, wildcard: bool = False):
        if wildcard:
            text_filter = or_(
                _text_like(cls.title, title, wildcard=True),
                _text_like(cls.src, title, wildcard=True),
                _text_like(cls.dest, title, wildcard=True),
            )
        else:
            text_filter = or_(
                _text_like(cls.title, f'%{title}%'),
                _text_like(cls.src, f'%{title}%'),
                _text_like(cls.dest, f'%{title}%'),
            )
        stmt = select(func.count(cls.id)).filter(text_filter)
        if status is not None:
            stmt = stmt.filter(cls.status == status)
        result = await db.execute(stmt)
        return result.scalar()

    @classmethod
    @legacy_db_query
    def list_by(cls, db: Session, mtype: Optional[str] = None, title: Optional[str] = None, year: Optional[str] = None,
                season: Optional[str] = None,
                episode: Optional[str] = None,
                media_source: Optional[MediaSource] = None,
                media_id: Optional[str] = None,
                dest: Optional[str] = None):
        """
        按媒体身份、季集或标题年份查询整理记录。
        """
        if media_source and media_id and mtype:
            statement = select(cls).where(cls.media_source == str(media_source),
                                          cls.media_id == str(media_id),
                                          cls.type == mtype)
        elif title and year:
            statement = select(cls).where(cls.title == title,
                                          cls.year == year)
        elif mtype and season is not None and dest:
            # 类型 + 转移路径（媒体服务器 webhook 缺少远端身份场景）
            return list(db.execute(select(cls).where(cls.type == mtype,
                                                cls.seasons == season,
                                                cls.dest.like(f"{dest}%"))).scalars().all())
        else:
            return []
        if season is not None and episode:
            # 电视剧某季某集：目标路径同样参与匹配，dest 为空即匹配空目标
            statement = statement.where(cls.seasons == season,
                                        cls.episodes == episode,
                                        cls.dest == dest)
        elif season is not None:
            # 电视剧某季
            statement = statement.where(cls.seasons == season)
        elif dest:
            # 电影：没有季集，用目标路径区分不同版本
            statement = statement.where(cls.dest == dest)
        return list(db.execute(statement).scalars().all())

    @classmethod
    @legacy_db_query
    def get_by_media_identity(
            cls, db: Session, media_source: MediaSource, media_id: str,
            mtype: Optional[str] = None,
    ):
        """按规范媒体身份和类型查询整理记录。"""
        return db.execute(select(cls).where(
            cls.media_source == str(media_source),
            cls.media_id == str(media_id),
            cls.type == mtype,
        )).scalars().first()

    @classmethod
    def update_download_hash(cls, db: Session, historyid: Optional[int] = None, download_hash: Optional[str] = None):
        """在调用方事务中暂存下载任务哈希更新。"""
        db.execute(
            update(cls).where(cls.id == historyid).values(download_hash=download_hash)
        )

    @classmethod
    def replace_by_src(cls, db: Session, **kwargs) -> "TransferHistory":
        """
        用同源存储的新记录原子替换旧整理历史。

        同一源路径在一个存储中只能对应一条最新整理记录。先在同一事务内清理旧行再
        插入，避免旧的“查询一条再删除一条”在遗留重复数据下留下脏记录。
        :param db: 数据库会话
        :param kwargs: 整理历史字段
        :return: 新创建的整理历史
        """
        src = kwargs.get("src")
        src_storage = kwargs.get("src_storage") or "local"
        kwargs["src_storage"] = src_storage
        if src:
            db.execute(
                delete(cls).where(
                    cls.src == src,
                    cls.src_storage == src_storage,
                ),
                execution_options={"synchronize_session": False},
            )
        history = cls(**kwargs)
        db.add(history)
        db.flush()
        return history

    @classmethod
    @legacy_db_query
    def list_by_date(cls, db: Session, date: str):
        """
        查询某时间之后的转移历史
        """
        return list(db.execute(
            select(cls).where(cls.date > date).order_by(cls.id.desc())
        ).scalars().all())

    @classmethod
    def delete_before(
        cls,
        db: Session,
        before_time: str,
        limit: Optional[int] = 500,
    ) -> int:
        """
        分批删除指定时间之前的整理历史。
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
