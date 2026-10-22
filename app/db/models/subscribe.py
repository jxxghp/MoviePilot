"""
影视与音乐订阅配置及状态的 ORM 模型与会话内数据操作。

会话及事务由调用方管理，本模块不负责提交或回滚。
"""

import hashlib
import json
import time
from typing import Any, Optional, Self
from uuid import uuid4

from sqlalchemy import JSON, ColumnElement, Float, Index, Integer, String, event, inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column
from app.db.models._constraints import media_identity_constraint
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource


def subscription_identity_key(values: dict[str, Any]) -> Optional[str]:
    """
    为完整订阅身份生成数据库仲裁键，保留用户名大小写及空值区别。

    空字符串 owner 沿用显式跳过查重的兼容合同；旧单曲的空 music_type 按 recording 处理。

    :param values: 已规范化的持久化字段
    :return: SHA256 身份摘要；身份不完整或显式跳过 owner 查重时为 None
    """
    if not values.get("media_source") or not values.get("media_id") or not values.get("type"):
        return None
    if values.get("username") == "":
        return None
    identity = [values.get(field) for field in (
        "username", "type", "media_source", "media_id", "season", "episode_group", "music_type",
    )]
    if identity[1] == "音乐" and not identity[-1]:
        identity[-1] = MUSIC_ENTITY_RECORDING
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class Subscribe(Base):
    """
    订阅表
    """

    id = get_id_column()
    # 不随整数主键复用的业务活动标识，完成时原样带入订阅历史。
    activity_key: Mapped[Optional[str]] = mapped_column(String(32), default=lambda: uuid4().hex)
    # 完整身份的数据库唯一仲裁键；迁移保留的旧重复记录为空，不删除其业务事实。
    identity_key: Mapped[Optional[str]] = mapped_column(String(64))
    # 标题
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # 年份
    year: Mapped[Optional[str]] = mapped_column(String)
    # 类型
    type: Mapped[Optional[str]] = mapped_column(String)
    # 自定义定时搜索间隔（小时）；空值跟随系统
    search_interval: Mapped[Optional[int]] = mapped_column(Integer)
    # 搜索关键字
    keyword: Mapped[Optional[str]] = mapped_column(String)
    media_source: Mapped[Optional[str]] = mapped_column(String, index=True)
    media_id: Mapped[Optional[str]] = mapped_column(String, index=True)
    # 音乐实体类型：recording 单曲、album 专辑
    music_type: Mapped[Optional[str]] = mapped_column(String)
    # 专辑预期总曲目数，作为分批下载完成判定的分母
    total_tracks: Mapped[Optional[int]] = mapped_column(Integer)
    # 已接受资源中可识别音轨的稳定键集合，供专辑订阅跨轮次累计进度
    downloaded_tracks: Mapped[Optional[Any]] = mapped_column(JSON)
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
    # 最近一次主动搜索开始时间，使用带时区的 UTC 时间
    last_search: Mapped[Optional[str]] = mapped_column(String)
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
        Index("uq_subscribe_identity_key", "identity_key", unique=True),
        media_identity_constraint("subscribe"),
        Index("ix_subscribe_type_date", "type", "date"),
        Index("ix_subscribe_media_identity", "media_source", "media_id"),
    )

    @classmethod
    def _identity_condition(
        cls,
        media_source: MediaSource | str | None = None,
        media_id: Optional[str] = None,
        music_type: Optional[str] = None,
    ) -> Optional[ColumnElement[bool]]:
        """
        按统一媒体身份优先级构造订阅查询条件。
        """
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
        mtype: Optional[str] = None,
    ) -> Optional[Self]:
        """
        按媒体身份、季号与剧集组查询已有订阅。
        """
        condition = cls._identity_condition(media_source, media_id, music_type)
        if condition is None:
            return None
        statement = select(cls).where(condition)
        if mtype is not None:
            statement = statement.where(cls.type == mtype)
        if season is not None:
            statement = statement.where(cls.season == season)
        subscription: Optional[Self] = db.execute(statement.where(cls.episode_group == episode_group)).scalars().first()
        return subscription

    @classmethod
    async def async_exists(
        cls,
        db: AsyncSession,
        media_source: MediaSource | str | None = None,
        media_id: str | None = None,
        season: Optional[int] = None,
        episode_group: Optional[str] = None,
        music_type: Optional[str] = None,
        mtype: Optional[str] = None,
    ) -> Optional[Self]:
        """
        异步按媒体身份、季号与剧集组查询已有订阅。
        """
        condition = cls._identity_condition(media_source, media_id, music_type)
        if condition is None:
            return None
        statement = select(cls).where(condition)
        if mtype is not None:
            statement = statement.where(cls.type == mtype)
        if season is not None:
            statement = statement.where(cls.season == season)
        result = await db.execute(statement.where(cls.episode_group == episode_group))
        subscription: Optional[Self] = result.scalars().first()
        return subscription

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
        mtype: Optional[str] = None,
    ) -> Optional[Self]:
        """
        按订阅 owner、媒体身份、季号与剧集组查询订阅行。
        """
        if not username:
            return None
        condition = cls._identity_condition(media_source, media_id, music_type)
        if condition is None:
            return None
        statement = select(cls).where(cls.username == username, condition)
        if mtype is not None:
            statement = statement.where(cls.type == mtype)
        if season is not None:
            statement = statement.where(cls.season == season)
        subscription: Optional[Self] = db.execute(statement.where(cls.episode_group == episode_group)).scalars().first()
        return subscription

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
        mtype: Optional[str] = None,
    ) -> Optional[Self]:
        """
        异步按订阅 owner、媒体身份、季号与剧集组查询订阅行。
        """
        if not username:
            return None
        condition = cls._identity_condition(media_source, media_id, music_type)
        if condition is None:
            return None
        statement = select(cls).where(cls.username == username, condition)
        if mtype is not None:
            statement = statement.where(cls.type == mtype)
        if season is not None:
            statement = statement.where(cls.season == season)
        result = await db.execute(statement.where(cls.episode_group == episode_group))
        subscription: Optional[Self] = result.scalars().first()
        return subscription

    @classmethod
    def get_by_state(cls, db: Session, state: str | None = None) -> list[Self]:
        """
        在调用方 Session 中按状态列表查询订阅。
        """
        statement = select(cls)
        if state:
            statement = statement.where(cls.state.in_(state.split(",")))
        return list(db.execute(statement).scalars().all())

    @classmethod
    async def async_get_by_state(cls, db: AsyncSession, state: str | None = None) -> list[Self]:
        """
        在调用方 AsyncSession 中按状态列表查询订阅。
        """
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
    ) -> Optional[Self]:
        """
        在调用方 Session 中按标题查询订阅。
        """
        statement = select(cls).where(cls.name == title)
        if season is not None:
            statement = statement.where(cls.season == season)
        subscription: Optional[Self] = db.execute(statement).scalars().first()
        return subscription

    @classmethod
    async def async_get_by_title(
        cls,
        db: AsyncSession,
        title: str,
        season: Optional[int] = None,
    ) -> Optional[Self]:
        """
        在调用方 AsyncSession 中按标题查询订阅。
        """
        statement = select(cls).where(cls.name == title)
        if season is not None:
            statement = statement.where(cls.season == season)
        result = await db.execute(statement)
        subscription: Optional[Self] = result.scalars().first()
        return subscription

    @classmethod
    async def async_list_by_title(
        cls,
        db: AsyncSession,
        title: str,
        season: Optional[int] = None,
    ) -> list[Self]:
        """
        在调用方 AsyncSession 中按标题查询候选订阅列表。
        """
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
    ) -> list[Self]:
        """
        同步按统一媒体身份查询候选订阅列表。
        """
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
    ) -> list[Self]:
        """
        异步按统一媒体身份查询候选订阅列表。
        """
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
        episode_group: Optional[str] = None,
    ) -> Optional[Self]:
        """
        根据条件查询订阅
        """
        condition = cls._identity_condition(media_source, media_id, music_type)
        if condition is None:
            return None
        statement = select(cls).where(condition, cls.type == type, cls.episode_group == episode_group)
        if season is not None:
            statement = statement.where(cls.season == season)
        subscription: Optional[Self] = db.execute(statement).scalars().first()
        return subscription

    @classmethod
    async def async_get_by(
        cls,
        db: AsyncSession,
        type: str | MediaSource | None = None,
        media_source: MediaSource | str | None = None,
        media_id: str | None = None,
        season: Optional[int] = None,
        music_type: Optional[str] = None,
        episode_group: Optional[str] = None,
    ) -> Optional[Self]:
        """
        根据条件查询订阅
        """
        condition = cls._identity_condition(media_source, media_id, music_type)
        if condition is None:
            return None
        query = select(cls).filter(condition, cls.type == type, cls.episode_group == episode_group)
        if season is not None:
            query = query.filter(cls.season == season)
        result = await db.execute(query)
        subscription: Optional[Self] = result.scalars().first()
        return subscription

    @classmethod
    def list_by_username(cls, db: Session, username: str, state: Optional[str] = None, mtype: Optional[str] = None) -> list[Self]:
        """
        在调用方 Session 中按用户筛选订阅。
        """
        statement = select(cls).where(cls.username == username)
        if state:
            statement = statement.where(cls.state == state)
        if mtype:
            statement = statement.where(cls.type == mtype)
        return list(db.execute(statement).scalars().all())

    @classmethod
    async def async_list_by_username(
        cls, db: AsyncSession, username: str, state: Optional[str] = None, mtype: Optional[str] = None
    ) -> list[Self]:
        """
        在调用方 AsyncSession 中按用户筛选订阅。
        """
        statement = select(cls).where(cls.username == username)
        if state:
            statement = statement.where(cls.state == state)
        if mtype:
            statement = statement.where(cls.type == mtype)
        result = await db.execute(statement)
        return list(result.scalars().all())

    @classmethod
    def list_by_type(cls, db: Session, mtype: str, days: int = 7) -> list[Self]:
        """
        在调用方 Session 中按类型查询最近时间窗内的订阅。
        """
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
    async def async_list_by_type(cls, db: AsyncSession, mtype: str, days: int = 7) -> list[Self]:
        """
        在调用方 AsyncSession 中按类型查询最近时间窗内的订阅。
        """
        result = await db.execute(
            select(cls).where(
                cls.type == mtype,
                cls.date >= time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 86400 * int(days))),
            )
        )
        return list(result.scalars().all())


def _assign_subscription_identity(_mapper: Any, _connection: Any, record: Subscribe) -> None:
    """
    为 ORM 新建记录填充身份键，涵盖兼容 SDK 的直接模型写入。
    """
    record.identity_key = subscription_identity_key(record.__dict__)


def _refresh_subscription_identity(_mapper: Any, _connection: Any, record: Subscribe) -> None:
    """
    仅身份字段改变时更新唯一键，保留迁移留下的旧重复记录及其原有进度。
    """
    state = inspect(record)
    if any(state.attrs[field].history.has_changes() for field in (
        "username", "type", "media_source", "media_id", "season", "episode_group", "music_type",
    )):
        record.identity_key = subscription_identity_key(record.__dict__)


event.listen(Subscribe, "before_insert", _assign_subscription_identity)
event.listen(Subscribe, "before_update", _refresh_subscription_identity)
