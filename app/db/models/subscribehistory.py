from typing import Any, Optional

from sqlalchemy import JSON, Float, Index, Integer, String, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column
from app.db.models._constraints import media_identity_constraint
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource


class SubscribeHistory(Base):
    """
    订阅历史表
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
    # 专辑预期总曲目数
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
    # 订阅完成时间
    date: Mapped[Optional[str]] = mapped_column(String)
    # 订阅用户
    username: Mapped[Optional[str]] = mapped_column(String)
    # 订阅站点
    sites: Mapped[Optional[Any]] = mapped_column(JSON)
    # 是否洗版
    best_version: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 是否只洗全集整包，开启后电视剧洗版不按单集下载
    best_version_full: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 完成时的整体优先级
    current_priority: Mapped[Optional[int]] = mapped_column(Integer)
    # 完成时的音乐格式
    current_audio_format: Mapped[Optional[str]] = mapped_column(String)
    # 完成时的音乐码率（bps）
    current_bitrate: Mapped[Optional[int]] = mapped_column(Integer)
    # 完成时的音乐位深（bit）
    current_bit_depth: Mapped[Optional[int]] = mapped_column(Integer)
    # 完成时的音乐采样率（Hz）
    current_sample_rate: Mapped[Optional[int]] = mapped_column(Integer)
    # 洗版时已下载剧集的优先级状态，格式：{"1": 90, "2": 100}
    episode_priority: Mapped[Optional[Any]] = mapped_column(JSON)
    # 保存路径
    save_path: Mapped[Optional[str]] = mapped_column(String)
    # 是否使用 imdbid 搜索
    search_imdbid: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 自定义识别词
    custom_words: Mapped[Optional[str]] = mapped_column(String)
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
    # 过滤规则组
    filter_groups: Mapped[Optional[Any]] = mapped_column(JSON, default=list)
    # 剧集组
    episode_group: Mapped[Optional[str]] = mapped_column(String)

    __table_args__ = (
        media_identity_constraint("subscribehistory"),
        Index('ix_subscribehistory_type_date', 'type', 'date'),
        Index('ix_subscribehistory_date_id', 'date', 'id'),
        Index('ix_subscribehistory_media_identity', 'media_source', 'media_id'),
    )

    @classmethod
    def list_by_type(cls, db: Session, mtype: str, page: int = 1, count: int = 30):
        """在调用方 Session 中按媒体类型分页查询订阅历史。"""
        return list(db.execute(
            select(cls).where(
                cls.type == mtype
            ).order_by(
                cls.date.desc()
            ).offset((page - 1) * count).limit(count)
        ).scalars().all())

    @classmethod
    async def async_list_by_type(cls, db: AsyncSession, mtype: str, page: int = 1, count: int = 30):
        """在调用方 AsyncSession 中按媒体类型分页查询订阅历史。"""
        result = await db.execute(
            select(cls).filter(
                cls.type == mtype
            ).order_by(
                cls.date.desc()
            ).offset((page - 1) * count).limit(count)
        )
        return list(result.scalars().all())

    @classmethod
    async def async_list_by_type_and_username(
            cls,
            db: AsyncSession,
            mtype: str,
            username: str,
            page: int = 1,
            count: int = 30
    ):
        """
        按订阅 owner 查询指定类型的历史分页。
        """
        if not username:
            return []
        result = await db.execute(
            select(cls).filter(
                cls.type == mtype,
                cls.username == username
            ).order_by(
                cls.date.desc()
            ).offset((page - 1) * count).limit(count)
        )
        return list(result.scalars().all())

    @classmethod
    async def async_count_by_type(cls, db: AsyncSession, mtype: str) -> int:
        """统计指定媒体类型的订阅历史。"""
        result = await db.execute(
            select(func.count(cls.id)).where(cls.type == mtype)
        )
        return int(result.scalar() or 0)

    @classmethod
    async def async_count_by_type_and_username(
        cls,
        db: AsyncSession,
        mtype: str,
        username: str,
    ) -> int:
        """统计指定媒体类型和 owner 的订阅历史。"""
        if not username:
            return 0
        result = await db.execute(
            select(func.count(cls.id)).where(
                cls.type == mtype,
                cls.username == username,
            )
        )
        return int(result.scalar() or 0)

    @classmethod
    def _identity_condition(
            cls,
            media_source: MediaSource | str | None = None,
            media_id: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """按统一媒体身份优先级构造订阅历史查询条件。"""
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
    def exists(
            cls, db: Session, media_source: MediaSource | str, media_id: str,
            season: Optional[int] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """按媒体身份、季号及可选剧集组查询订阅历史。"""
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        statement = select(cls).where(condition)
        if season is not None:
            statement = statement.where(cls.season == season)
        statement = statement.where(cls.episode_group == episode_group)
        return db.execute(statement).scalars().first()

    @classmethod
    async def async_exists(
            cls, db: AsyncSession, media_source: MediaSource | str, media_id: str,
            season: Optional[int] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """异步按媒体身份、季号及可选剧集组查询订阅历史。"""
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        query = select(cls).filter(condition)
        if season is not None:
            query = query.filter(cls.season == season)
        query = query.filter(cls.episode_group == episode_group)
        result = await db.execute(query)
        return result.scalars().first()
