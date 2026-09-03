import time
from typing import Any, Optional

from sqlalchemy import JSON, Float, Index, Integer, String, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column
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
    state: Mapped[str] = mapped_column(String, nullable=False, index=True, default="N")
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
    # 自定义媒体类别稳定标识
    media_category_id: Mapped[Optional[str]] = mapped_column(String)
    # 自定义媒体类别兼容路径快照
    media_category: Mapped[Optional[str]] = mapped_column(String)
    # 过滤规则组
    filter_groups: Mapped[Optional[Any]] = mapped_column(JSON, default=list)
    # 选择的剧集组
    episode_group: Mapped[Optional[str]] = mapped_column(String)

    __table_args__ = (
        media_identity_constraint("subscribe"),
        Index("ix_subscribe_type_date", "type", "date"),
        Index("ix_subscribe_media_identity", "media_source", "media_id"),
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
        condition = (cls.media_source == str(media_source)) & (cls.media_id == str(media_id).strip())
        if music_type == MUSIC_ENTITY_RECORDING:
            return condition & or_(cls.music_type == music_type, cls.music_type.is_(None))
        if music_type:
            return condition & (cls.music_type == music_type)
        return condition

    @classmethod
    def exists(
        cls,
        db: Session,
        media_source: MediaSource | str | None = None,
        media_id: str | None = None,
        season: Optional[int] = None,
        episode_group: Optional[str] = None,
        music_type: Optional[str] = None,
    ):
        """按媒体身份、季号与剧集组查询已有订阅。"""
        condition = cls._identity_condition(media_source, media_id, music_type)
        if condition is None:
            return None
        statement = select(cls).where(condition)
        if season is not None:
            statement = statement.where(cls.season == season)
        return db.execute(statement.where(cls.episode_group == episode_group)).scalars().first()

    @classmethod
    async def async_exists(
        cls,
        db: AsyncSession,
        media_source: MediaSource | str | None = None,
        media_id: str | None = None,
        season: Optional[int] = None,
        episode_group: Optional[str] = None,
        music_type: Optional[str] = None,
    ):
        """异步按媒体身份、季号与剧集组查询已有订阅。"""
        condition = cls._identity_condition(media_source, media_id, music_type)
        if condition is None:
            return None
        statement = select(cls).where(condition)
        if season is not None:
            statement = statement.where(cls.season == season)
        result = await db.execute(statement.where(cls.episode_group == episode_group))
        return result.scalars().first()

    @classmethod
    def exists_by_username(
        cls,
        db: Session,
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
        if not username:
            return None
        condition = cls._identity_condition(media_source, media_id, music_type)
        if condition is None:
            return None
        statement = select(cls).where(cls.username == username, condition)
        if season is not None:
            statement = statement.where(cls.season == season)
        return db.execute(statement.where(cls.episode_group == episode_group)).scalars().first()

    @classmethod
    async def async_exists_by_username(
        cls,
        db: AsyncSession,
        username: str | MediaSource | None = None,
        media_source: MediaSource | str | None = None,
        media_id: str | None = None,
        season: Optional[int] = None,
        episode_group: Optional[str] = None,
        music_type: Optional[str] = None,
    ):
        """
        异步按订阅 owner、媒体身份、季号与剧集组查询订阅行。
        """
        if not username:
            return None
        condition = cls._identity_condition(media_source, media_id, music_type)
        if condition is None:
            return None
        statement = select(cls).where(cls.username == username, condition)
        if season is not None:
            statement = statement.where(cls.season == season)
        result = await db.execute(statement.where(cls.episode_group == episode_group))
        return result.scalars().first()

    @classmethod
    def get_by_state(cls, db: Session, state: str | None = None):
        """在调用方 Session 中按状态列表查询订阅。"""
        statement = select(cls)
        if state:
            statement = statement.where(cls.state.in_(state.split(",")))
        return list(db.execute(statement).scalars().all())

    @classmethod
    async def async_get_by_state(cls, db: AsyncSession, state: str | None = None):
        """在调用方 AsyncSession 中按状态列表查询订阅。"""
        statement = select(cls)
        if state:
            statement = statement.where(cls.state.in_(state.split(",")))
        result = await db.execute(statement)
        return list(result.scalars().all())

    @classmethod
    def get_by_title(
        cls,
        db: Session,
        title: str,
        season: Optional[int] = None,
    ):
        """在调用方 Session 中按标题查询订阅。"""
        statement = select(cls).where(cls.name == title)
        if season is not None:
            statement = statement.where(cls.season == season)
        return db.execute(statement).scalars().first()

    @classmethod
    async def async_get_by_title(
        cls,
        db: AsyncSession,
        title: str,
        season: Optional[int] = None,
    ):
        """在调用方 AsyncSession 中按标题查询订阅。"""
        statement = select(cls).where(cls.name == title)
        if season is not None:
            statement = statement.where(cls.season == season)
        result = await db.execute(statement)
        return result.scalars().first()

    @classmethod
    async def async_list_by_title(
        cls,
        db: AsyncSession,
        title: str,
        season: Optional[int] = None,
    ):
        """在调用方 AsyncSession 中按标题查询候选订阅列表。"""
        statement = select(cls).where(cls.name == title)
        if season is not None:
            statement = statement.where(cls.season == season)
        result = await db.execute(statement)
        return list(result.scalars().all())

    @classmethod
    def list_by_media_identity(
        cls,
        db: Session,
        media_source: MediaSource | str | None = None,
        media_id: str | None = None,
        music_type: Optional[str] = None,
    ):
        """同步按统一媒体身份查询候选订阅列表。"""
        condition = cls._identity_condition(
            media_source=media_source,
            media_id=media_id,
            music_type=music_type,
        )
        if condition is None:
            return []
        return list(db.execute(select(cls).where(condition)).scalars().all())

    @classmethod
    async def async_list_by_media_identity(
        cls,
        db: AsyncSession,
        media_source: MediaSource | str | None = None,
        media_id: str | None = None,
        music_type: Optional[str] = None,
    ):
        """异步按统一媒体身份查询候选订阅列表。"""
        condition = cls._identity_condition(
            media_source=media_source,
            media_id=media_id,
            music_type=music_type,
        )
        if condition is None:
            return []
        result = await db.execute(select(cls).where(condition))
        return list(result.scalars().all())

    @classmethod
    def get_by(
        cls,
        db: Session,
        type: str | MediaSource | None = None,
        media_source: MediaSource | str | None = None,
        media_id: str | None = None,
        season: Optional[int] = None,
        music_type: Optional[str] = None,
    ):
        """
        根据条件查询订阅
        """
        condition = cls._identity_condition(media_source, media_id, music_type)
        if condition is None:
            return None
        statement = select(cls).where(condition, cls.type == type)
        if season is not None:
            statement = statement.where(cls.season == season)
        return db.execute(statement).scalars().first()

    @classmethod
    async def async_get_by(
        cls,
        db: AsyncSession,
        type: str | MediaSource | None = None,
        media_source: MediaSource | str | None = None,
        media_id: str | None = None,
        season: Optional[int] = None,
        music_type: Optional[str] = None,
    ):
        """
        根据条件查询订阅
        """
        condition = cls._identity_condition(media_source, media_id, music_type)
        if condition is None:
            return None
        query = select(cls).filter(condition, cls.type == type)
        if season is not None:
            query = query.filter(cls.season == season)
        result = await db.execute(query)
        return result.scalars().first()

    @classmethod
    def list_by_username(cls, db: Session, username: str, state: Optional[str] = None, mtype: Optional[str] = None):
        """在调用方 Session 中按用户筛选订阅。"""
        statement = select(cls).where(cls.username == username)
        if state:
            statement = statement.where(cls.state == state)
        if mtype:
            statement = statement.where(cls.type == mtype)
        return list(db.execute(statement).scalars().all())

    @classmethod
    async def async_list_by_username(
        cls, db: AsyncSession, username: str, state: Optional[str] = None, mtype: Optional[str] = None
    ):
        """在调用方 AsyncSession 中按用户筛选订阅。"""
        statement = select(cls).where(cls.username == username)
        if state:
            statement = statement.where(cls.state == state)
        if mtype:
            statement = statement.where(cls.type == mtype)
        result = await db.execute(statement)
        return list(result.scalars().all())

    @classmethod
    def list_by_type(cls, db: Session, mtype: str, days: int = 7):
        """在调用方 Session 中按类型查询最近时间窗内的订阅。"""
        return list(
            db.execute(
                select(cls).where(
                    cls.type == mtype,
                    cls.date >= time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 86400 * int(days))),
                )
            )
            .scalars()
            .all()
        )

    @classmethod
    async def async_list_by_type(cls, db: AsyncSession, mtype: str, days: int = 7):
        """在调用方 AsyncSession 中按类型查询最近时间窗内的订阅。"""
        result = await db.execute(
            select(cls).where(
                cls.type == mtype,
                cls.date >= time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 86400 * int(days))),
            )
        )
        return list(result.scalars().all())
