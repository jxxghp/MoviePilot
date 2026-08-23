import time
from typing import Any, Optional

from sqlalchemy import Integer, String, Float, JSON, Index, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import get_id_column, Base
from app.db.decorators import legacy_async_db_query, legacy_db_query
from app.db.models._constraints import media_identity_constraint
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource


class Subscribe(Base):
    """
    订阅表
    """
    id = get_id_column()
    # 标题
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # 年份
    year: Mapped[Optional[str]] = mapped_column(String)
    # 类型
    type: Mapped[Optional[str]] = mapped_column(String)
    # 搜索关键字
    keyword: Mapped[Optional[str]] = mapped_column(String)
    media_source: Mapped[Optional[str]] = mapped_column(String, index=True)
    media_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 音乐实体类型：recording 单曲、album 专辑
    music_type: Mapped[Optional[str]] = mapped_column(String)
    # 专辑预期总曲目数，供整专资源完整性判断
    total_tracks: Mapped[Optional[int]] = mapped_column(Integer)
    # 季号
    season: Mapped[Optional[int]] = mapped_column(Integer)
    # 海报
    poster: Mapped[Optional[str]] = mapped_column(String)
    # 背景图
    backdrop: Mapped[Optional[str]] = mapped_column(String)
    # 评分，float
    vote: Mapped[Optional[float]] = mapped_column(Float)
    # 简介
    description: Mapped[Optional[str]] = mapped_column(String)
    # 过滤规则
    filter: Mapped[Optional[str]] = mapped_column(String)
    # 包含
    include: Mapped[Optional[str]] = mapped_column(String)
    # 排除
    exclude: Mapped[Optional[str]] = mapped_column(String)
    # 质量
    quality: Mapped[Optional[str]] = mapped_column(String)
    # 分辨率
    resolution: Mapped[Optional[str]] = mapped_column(String)
    # 特效
    effect: Mapped[Optional[str]] = mapped_column(String)
    # 音乐音质等级：hires/lossless/lossy，可用正则组合
    audio_quality: Mapped[Optional[str]] = mapped_column(String)
    # 音频格式，可用正则组合
    audio_format: Mapped[Optional[str]] = mapped_column(String)
    # 最低码率（bps）
    min_bitrate: Mapped[Optional[int]] = mapped_column(Integer)
    # 最低位深（bit）
    min_bit_depth: Mapped[Optional[int]] = mapped_column(Integer)
    # 最低采样率（Hz）
    min_sample_rate: Mapped[Optional[int]] = mapped_column(Integer)
    # 总集数
    total_episode: Mapped[Optional[int]] = mapped_column(Integer)
    # 开始集数
    start_episode: Mapped[Optional[int]] = mapped_column(Integer)
    # 缺失集数
    lack_episode: Mapped[Optional[int]] = mapped_column(Integer)
    # 附加信息
    note: Mapped[Optional[Any]] = mapped_column(JSON)
    # 状态：N-新建 R-订阅中 P-待定 S-暂停
    state: Mapped[str] = mapped_column(String, nullable=False, index=True, default='N')
    # 最后更新时间
    last_update: Mapped[Optional[str]] = mapped_column(String)
    # 创建时间
    date: Mapped[Optional[str]] = mapped_column(String)
    # 订阅用户
    username: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 订阅站点
    sites: Mapped[Optional[Any]] = mapped_column(JSON, default=list)
    # 下载器
    downloader: Mapped[Optional[str]] = mapped_column(String)
    # 是否洗版
    best_version: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 是否只洗全集整包，开启后电视剧洗版不按单集下载
    best_version_full: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 当前优先级
    current_priority: Mapped[Optional[int]] = mapped_column(Integer)
    # 当前音乐版本格式
    current_audio_format: Mapped[Optional[str]] = mapped_column(String)
    # 当前音乐版本码率（bps）
    current_bitrate: Mapped[Optional[int]] = mapped_column(Integer)
    # 当前音乐版本位深（bit）
    current_bit_depth: Mapped[Optional[int]] = mapped_column(Integer)
    # 当前音乐版本采样率（Hz）
    current_sample_rate: Mapped[Optional[int]] = mapped_column(Integer)
    # 洗版时已下载剧集的优先级状态，格式：{"1": 90, "2": 100}
    episode_priority: Mapped[Optional[Any]] = mapped_column(JSON)
    # 保存路径
    save_path: Mapped[Optional[str]] = mapped_column(String)
    # 是否使用 imdbid 搜索
    search_imdbid: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 是否手动修改过总集数 0否 1是
    manual_total_episode: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 自定义识别词
    custom_words: Mapped[Optional[str]] = mapped_column(String)
    # 自定义媒体类别
    media_category: Mapped[Optional[str]] = mapped_column(String)
    # 过滤规则组
    filter_groups: Mapped[Optional[Any]] = mapped_column(JSON, default=list)
    # 选择的剧集组
    episode_group: Mapped[Optional[str]] = mapped_column(String)

    __table_args__ = (
        media_identity_constraint("subscribe"),
        Index('ix_subscribe_type_date', 'type', 'date'),
        Index('ix_subscribe_media_identity', 'media_source', 'media_id'),
    )

    @classmethod
    def _identity_condition(
            cls,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """按统一媒体身份优先级构造订阅查询条件。"""
        if not media_source or media_id is None or not str(media_id).strip():
            return None
        condition = (
            (cls.media_source == str(media_source))
            & (cls.media_id == str(media_id).strip())
        )
        if music_type == MUSIC_ENTITY_RECORDING:
            return condition & or_(cls.music_type == music_type, cls.music_type.is_(None))
        if music_type:
            return condition & (cls.music_type == music_type)
        return condition

    @classmethod
    @legacy_db_query
    def exists(
            cls, db: Session | MediaSource | None = None,
            media_source: MediaSource | str | None = None,
            media_id: str | None = None,
            season: Optional[int] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """按媒体身份、季号与剧集组查询已有订阅。"""
        if db is not None and not isinstance(db, Session):
            media_source, media_id, db = db, media_source, None
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        def query(session: Session):
            """在给定会话中执行订阅身份查询。"""
            statement = select(cls).where(condition)
            if season is not None:
                statement = statement.where(cls.season == season)
            return session.execute(
                statement.where(cls.episode_group == episode_group)
            ).scalars().first()
        return query(db)

    @classmethod
    @legacy_async_db_query
    async def async_exists(
            cls, db: AsyncSession | MediaSource | None = None,
            media_source: MediaSource | str | None = None,
            media_id: str | None = None,
            season: Optional[int] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """异步按媒体身份、季号与剧集组查询已有订阅。"""
        if db is not None and not isinstance(db, AsyncSession):
            media_source, media_id, db = db, media_source, None
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        async def query(session: AsyncSession):
            """在给定异步会话中执行订阅身份查询。"""
            statement = select(cls).where(condition)
            if season is not None:
                statement = statement.where(cls.season == season)
            result = await session.execute(
                statement.where(cls.episode_group == episode_group)
            )
            return result.scalars().first()
        return await query(db)

    @classmethod
    @legacy_db_query
    def exists_by_username(
            cls, db: Session | str | None = None,
            username: str | MediaSource | None = None,
            media_source: MediaSource | str | None = None,
            media_id: str | None = None,
            season: Optional[int] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """
        按订阅 owner、媒体身份、季号与剧集组查询订阅行。
        """
        if db is not None and not isinstance(db, Session):
            username, media_source, media_id, db = db, username, media_source, None
        if not username:
            return None
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        def query(session: Session):
            """在给定会话中执行订阅 owner 查询。"""
            statement = select(cls).where(cls.username == username, condition)
            if season is not None:
                statement = statement.where(cls.season == season)
            return session.execute(
                statement.where(cls.episode_group == episode_group)
            ).scalars().first()
        return query(db)

    @classmethod
    @legacy_async_db_query
    async def async_exists_by_username(
            cls, db: AsyncSession | str | None = None,
            username: str | MediaSource | None = None,
            media_source: MediaSource | str | None = None,
            media_id: str | None = None, season: Optional[int] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """
        异步按订阅 owner、媒体身份、季号与剧集组查询订阅行。
        """
        if db is not None and not isinstance(db, AsyncSession):
            username, media_source, media_id, db = db, username, media_source, None
        if not username:
            return None
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        async def query(session: AsyncSession):
            """在给定异步会话中执行订阅 owner 查询。"""
            statement = select(cls).where(cls.username == username, condition)
            if season is not None:
                statement = statement.where(cls.season == season)
            result = await session.execute(
                statement.where(cls.episode_group == episode_group)
            )
            return result.scalars().first()
        return await query(db)

    @classmethod
    @legacy_db_query
    def get_by_state(cls, db: Session | str | None = None, state: str | None = None):
        """按状态列表查询订阅，兼容显式会话和旧插件无会话调用。"""
        if not isinstance(db, Session):
            state, db = db if state is None else state, None
        def query(session: Session):
            """在给定会话中执行状态查询。"""
            statement = select(cls)
            if state:
                statement = statement.where(cls.state.in_(state.split(',')))
            return list(session.execute(statement).scalars().all())
        return query(db)

    @classmethod
    @legacy_async_db_query
    async def async_get_by_state(
        cls, db: AsyncSession | str | None = None, state: str | None = None
    ):
        """异步按状态列表查询订阅，兼容显式会话和旧插件无会话调用。"""
        if not isinstance(db, AsyncSession):
            state, db = db if state is None else state, None
        async def query(session: AsyncSession):
            """在给定异步会话中执行状态查询。"""
            statement = select(cls)
            if state:
                statement = statement.where(cls.state.in_(state.split(',')))
            result = await session.execute(statement)
            return list(result.scalars().all())
        return await query(db)

    @classmethod
    @legacy_db_query
    def get_by_title(
        cls, db: Session | str | None = None, title: str | None = None,
        season: Optional[int] = None,
    ):
        """按标题查询订阅，兼容显式会话和旧插件无会话调用。"""
        if not isinstance(db, Session):
            title, db = db if title is None else title, None
        def query(session: Session):
            """在给定会话中执行标题查询。"""
            statement = select(cls).where(cls.name == title)
            if season is not None:
                statement = statement.where(cls.season == season)
            return session.execute(statement).scalars().first()
        return query(db)

    @classmethod
    @legacy_async_db_query
    async def async_get_by_title(
        cls, db: AsyncSession | str | None = None, title: str | None = None,
        season: Optional[int] = None,
    ):
        """异步按标题查询订阅，兼容显式会话和旧插件无会话调用。"""
        if not isinstance(db, AsyncSession):
            title, db = db if title is None else title, None
        async def query(session: AsyncSession):
            """在给定异步会话中执行标题查询。"""
            statement = select(cls).where(cls.name == title)
            if season is not None:
                statement = statement.where(cls.season == season)
            result = await session.execute(statement)
            return result.scalars().first()
        return await query(db)

    @classmethod
    @legacy_async_db_query
    async def async_list_by_title(
        cls, db: AsyncSession | str | None = None, title: str | None = None,
        season: Optional[int] = None,
    ):
        """异步按标题查询候选订阅列表，兼容显式会话和旧插件无会话调用。"""
        if not isinstance(db, AsyncSession):
            title, db = db if title is None else title, None
        async def query(session: AsyncSession):
            """在给定异步会话中执行标题列表查询。"""
            statement = select(cls).where(cls.name == title)
            if season is not None:
                statement = statement.where(cls.season == season)
            result = await session.execute(statement)
            return list(result.scalars().all())
        return await query(db)

    @classmethod
    @legacy_db_query
    def list_by_media_identity(
            cls, db: Session | MediaSource | None = None,
            media_source: MediaSource | str | None = None,
            media_id: str | None = None,
            music_type: Optional[str] = None,
    ):
        """同步按统一媒体身份查询候选订阅列表。"""
        if db is not None and not isinstance(db, Session):
            media_source, media_id, db = db, media_source, None
        condition = cls._identity_condition(
            media_source=media_source,
            media_id=media_id,
            music_type=music_type,
        )
        if condition is None:
            return []
        def query(session: Session):
            """在给定会话中执行媒体身份列表查询。"""
            return list(session.execute(select(cls).where(condition)).scalars().all())
        return query(db)

    @classmethod
    @legacy_async_db_query
    async def async_list_by_media_identity(
            cls, db: AsyncSession | MediaSource | None = None,
            media_source: MediaSource | str | None = None,
            media_id: str | None = None,
            music_type: Optional[str] = None,
    ):
        """异步按统一媒体身份查询候选订阅列表。"""
        if db is not None and not isinstance(db, AsyncSession):
            media_source, media_id, db = db, media_source, None
        condition = cls._identity_condition(
            media_source=media_source,
            media_id=media_id,
            music_type=music_type,
        )
        if condition is None:
            return []
        async def query(session: AsyncSession):
            """在给定异步会话中执行媒体身份列表查询。"""
            result = await session.execute(select(cls).where(condition))
            return list(result.scalars().all())
        return await query(db)

    @classmethod
    @legacy_db_query
    def get_by(
            cls, db: Session | str | None = None,
            type: str | MediaSource | None = None,
            media_source: MediaSource | str | None = None,
            media_id: str | None = None,
            season: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """
        根据条件查询订阅
        """
        if db is not None and not isinstance(db, Session):
            type, media_source, media_id, db = db, type, media_source, None
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        statement = select(cls).where(condition, cls.type == type)
        if season is not None:
            statement = statement.where(cls.season == season)
        def query(session: Session):
            """在给定会话中执行类型媒体查询。"""
            return session.execute(statement).scalars().first()
        return query(db)

    @classmethod
    @legacy_async_db_query
    async def async_get_by(
            cls, db: AsyncSession | str | None = None,
            type: str | MediaSource | None = None,
            media_source: MediaSource | str | None = None,
            media_id: str | None = None,
            season: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """
        根据条件查询订阅
        """
        if db is not None and not isinstance(db, AsyncSession):
            type, media_source, media_id, db = db, type, media_source, None
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        query = select(cls).filter(condition, cls.type == type)
        if season is not None:
            query = query.filter(cls.season == season)
        async def execute_query(session: AsyncSession):
            """在给定异步会话中执行类型媒体查询。"""
            result = await session.execute(query)
            return result.scalars().first()
        return await execute_query(db)

    @classmethod
    @legacy_db_query
    def list_by_username(cls, db: Session | str | None = None, username: str | None = None,
                         state: Optional[str] = None, mtype: Optional[str] = None):
        """按用户筛选订阅，兼容显式会话和旧插件无会话调用。"""
        if not isinstance(db, Session):
            username, db = db if username is None else username, None
        def query(session: Session):
            """在给定会话中执行用户筛选查询。"""
            statement = select(cls).where(cls.username == username)
            if state:
                statement = statement.where(cls.state == state)
            if mtype:
                statement = statement.where(cls.type == mtype)
            return list(session.execute(statement).scalars().all())
        return query(db)

    @classmethod
    @legacy_async_db_query
    async def async_list_by_username(cls, db: AsyncSession | str | None = None,
                                     username: str | None = None, state: Optional[str] = None,
                                     mtype: Optional[str] = None):
        """异步按用户筛选订阅，兼容显式会话和旧插件无会话调用。"""
        if not isinstance(db, AsyncSession):
            username, db = db if username is None else username, None
        async def query(session: AsyncSession):
            """在给定异步会话中执行用户筛选查询。"""
            statement = select(cls).where(cls.username == username)
            if state:
                statement = statement.where(cls.state == state)
            if mtype:
                statement = statement.where(cls.type == mtype)
            result = await session.execute(statement)
            return list(result.scalars().all())
        return await query(db)

    @classmethod
    @legacy_db_query
    def list_by_type(cls, db: Session | str | None = None, mtype: str | None = None, days: int = 7):
        """按类型查询最近时间窗内的订阅，兼容显式会话和旧插件无会话调用。"""
        if not isinstance(db, Session):
            mtype, db = db if mtype is None else mtype, None
        def query(session: Session):
            """在给定会话中执行时间窗订阅查询。"""
            return list(session.execute(select(cls).where(
                cls.type == mtype,
                cls.date >= time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(time.time() - 86400 * int(days)))
            )).scalars().all())
        return query(db)

    @classmethod
    @legacy_async_db_query
    async def async_list_by_type(cls, db: AsyncSession | str | None = None,
                                 mtype: str | None = None, days: int = 7):
        """异步按类型查询最近时间窗内的订阅，兼容显式会话和旧插件无会话调用。"""
        if not isinstance(db, AsyncSession):
            mtype, db = db if mtype is None else mtype, None
        async def query(session: AsyncSession):
            """在给定异步会话中执行时间窗订阅查询。"""
            result = await session.execute(select(cls).where(
                cls.type == mtype,
                cls.date >= time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(time.time() - 86400 * int(days)))
            ))
            return list(result.scalars().all())
        return await query(db)
