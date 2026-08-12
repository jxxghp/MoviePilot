import time
from typing import Optional

from sqlalchemy import Column, Integer, String, Float, JSON, Index, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import db_query, db_update, get_id_column, Base, async_db_query, async_db_update
from app.db.models.media_identity import media_identity_constraint
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource


class Subscribe(Base):
    """
    订阅表
    """
    id = get_id_column()
    # 标题
    name = Column(String, nullable=False, index=True)
    # 年份
    year = Column(String)
    # 类型
    type = Column(String)
    # 搜索关键字
    keyword = Column(String)
    media_source = Column(String, index=True)
    media_id = Column(String, index=True)
    # 音乐实体类型：recording 单曲、album 专辑
    music_type = Column(String)
    # 专辑预期总曲目数，供整专资源完整性判断
    total_tracks = Column(Integer)
    # 季号
    season = Column(Integer)
    # 海报
    poster = Column(String)
    # 背景图
    backdrop = Column(String)
    # 评分，float
    vote = Column(Float)
    # 简介
    description = Column(String)
    # 过滤规则
    filter = Column(String)
    # 包含
    include = Column(String)
    # 排除
    exclude = Column(String)
    # 质量
    quality = Column(String)
    # 分辨率
    resolution = Column(String)
    # 特效
    effect = Column(String)
    # 音乐音质等级：hires/lossless/lossy，可用正则组合
    audio_quality = Column(String)
    # 音频格式，可用正则组合
    audio_format = Column(String)
    # 最低码率（bps）
    min_bitrate = Column(Integer)
    # 最低位深（bit）
    min_bit_depth = Column(Integer)
    # 最低采样率（Hz）
    min_sample_rate = Column(Integer)
    # 总集数
    total_episode = Column(Integer)
    # 开始集数
    start_episode = Column(Integer)
    # 缺失集数
    lack_episode = Column(Integer)
    # 附加信息
    note = Column(JSON)
    # 状态：N-新建 R-订阅中 P-待定 S-暂停
    state = Column(String, nullable=False, index=True, default='N')
    # 最后更新时间
    last_update = Column(String)
    # 创建时间
    date = Column(String)
    # 订阅用户
    username = Column(String, index=True)
    # 订阅站点
    sites = Column(JSON, default=list)
    # 下载器
    downloader = Column(String)
    # 是否洗版
    best_version = Column(Integer, default=0)
    # 是否只洗全集整包，开启后电视剧洗版不按单集下载
    best_version_full = Column(Integer, default=0)
    # 当前优先级
    current_priority = Column(Integer)
    # 当前音乐版本格式
    current_audio_format = Column(String)
    # 当前音乐版本码率（bps）
    current_bitrate = Column(Integer)
    # 当前音乐版本位深（bit）
    current_bit_depth = Column(Integer)
    # 当前音乐版本采样率（Hz）
    current_sample_rate = Column(Integer)
    # 洗版时已下载剧集的优先级状态，格式：{"1": 90, "2": 100}
    episode_priority = Column(JSON)
    # 保存路径
    save_path = Column(String)
    # 是否使用 imdbid 搜索
    search_imdbid = Column(Integer, default=0)
    # 是否手动修改过总集数 0否 1是
    manual_total_episode = Column(Integer, default=0)
    # 自定义识别词
    custom_words = Column(String)
    # 自定义媒体类别
    media_category = Column(String)
    # 过滤规则组
    filter_groups = Column(JSON, default=list)
    # 选择的剧集组
    episode_group = Column(String)

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
    @db_query
    def exists(
            cls, db: Session, media_source: MediaSource, media_id: str,
            season: Optional[int] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """按媒体身份、季号与剧集组查询已有订阅。"""
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        query = db.query(cls).filter(condition)
        if season is not None:
            query = query.filter(cls.season == season)
        query = query.filter(cls.episode_group == episode_group)
        return query.first()

    @classmethod
    @async_db_query
    async def async_exists(
            cls, db: AsyncSession, media_source: MediaSource, media_id: str,
            season: Optional[int] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """异步按媒体身份、季号与剧集组查询已有订阅。"""
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

    @classmethod
    @db_query
    def exists_by_username(
            cls, db: Session, username: str, media_source: MediaSource, media_id: str,
            season: Optional[int] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """
        按订阅 owner、媒体身份、季号与剧集组查询订阅行。
        """
        if not username:
            return None
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        query = db.query(cls).filter(cls.username == username, condition)
        if season is not None:
            query = query.filter(cls.season == season)
        query = query.filter(cls.episode_group == episode_group)
        return query.first()

    @classmethod
    @async_db_query
    async def async_exists_by_username(
            cls, db: AsyncSession, username: str, media_source: MediaSource,
            media_id: str, season: Optional[int] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """
        异步按订阅 owner、媒体身份、季号与剧集组查询订阅行。
        """
        if not username:
            return None
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        query = select(cls).filter(cls.username == username, condition)
        if season is not None:
            query = query.filter(cls.season == season)
        query = query.filter(cls.episode_group == episode_group)
        result = await db.execute(query)
        return result.scalars().first()

    @classmethod
    @db_query
    def get_by_state(cls, db: Session, state: str):
        # 如果 state 为空或 None，返回所有订阅
        if not state:
            return db.query(cls).all()
        else:
            # 如果传入的状态不为空，拆分成多个状态
            return db.query(cls).filter(cls.state.in_(state.split(','))).all()

    @classmethod
    @async_db_query
    async def async_get_by_state(cls, db: AsyncSession, state: str):
        # 如果 state 为空或 None，返回所有订阅
        if not state:
            result = await db.execute(select(cls))
        else:
            # 如果传入的状态不为空，拆分成多个状态
            result = await db.execute(
                select(cls).filter(cls.state.in_(state.split(',')))
            )
        return result.scalars().all()

    @classmethod
    @db_query
    def get_by_title(cls, db: Session, title: str, season: Optional[int] = None):
        if season is not None:
            return db.query(cls).filter(cls.name == title,
                                        cls.season == season).first()
        return db.query(cls).filter(cls.name == title).first()

    @classmethod
    @async_db_query
    async def async_get_by_title(cls, db: AsyncSession, title: str, season: Optional[int] = None):
        if season is not None:
            result = await db.execute(
                select(cls).filter(cls.name == title, cls.season == season)
            )
        else:
            result = await db.execute(
                select(cls).filter(cls.name == title)
            )
        return result.scalars().first()

    @classmethod
    @async_db_query
    async def async_list_by_title(cls, db: AsyncSession, title: str, season: Optional[int] = None):
        """
        异步按标题查询候选订阅列表。
        """
        if season is not None:
            result = await db.execute(
                select(cls).filter(cls.name == title, cls.season == season)
            )
        else:
            result = await db.execute(
                select(cls).filter(cls.name == title)
            )
        return result.scalars().all()

    @classmethod
    @db_query
    def list_by_media_identity(
            cls, db: Session, media_source: MediaSource, media_id: str,
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
        return db.query(cls).filter(condition).all()

    @classmethod
    @async_db_query
    async def async_list_by_media_identity(
            cls, db: AsyncSession, media_source: MediaSource, media_id: str,
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
        result = await db.execute(select(cls).filter(condition))
        return result.scalars().all()

    @classmethod
    @db_query
    def get_by(
            cls, db: Session, type: str, media_source: MediaSource, media_id: str,
            season: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """
        根据条件查询订阅
        """
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        query = db.query(cls).filter(condition, cls.type == type)
        if season is not None:
            query = query.filter(cls.season == season)
        return query.first()

    @classmethod
    @async_db_query
    async def async_get_by(
            cls, db: AsyncSession, type: str, media_source: MediaSource, media_id: str,
            season: Optional[str] = None,
            music_type: Optional[str] = None,
    ):
        """
        根据条件查询订阅
        """
        condition = cls._identity_condition(
            media_source, media_id, music_type
        )
        if condition is None:
            return None
        query = select(cls).filter(condition, cls.type == type)
        if season is not None:
            query = query.filter(cls.season == season)
        result = await db.execute(query)
        return result.scalars().first()

    @db_update
    def delete_by_media_identity(
            self, db: Session, media_source: MediaSource, media_id: str,
            season: Optional[int] = None,
    ) -> bool:
        """按规范媒体身份删除订阅。"""
        query = db.query(type(self)).filter(
            type(self).media_source == media_source,
            type(self).media_id == str(media_id),
        )
        if season is not None:
            query = query.filter(type(self).season == season)
        query.delete(synchronize_session=False)
        return True

    @async_db_update
    async def async_delete_by_media_identity(
            self, db: AsyncSession, media_source: MediaSource, media_id: str,
            season: Optional[int] = None,
    ) -> bool:
        """异步按规范媒体身份删除订阅。"""
        rows = await self.async_list_by_media_identity(
            db, media_source=media_source, media_id=media_id
        )
        for row in rows:
            if season is None or row.season == season:
                await row.async_delete(db, row.id)
        return True

    @classmethod
    @db_query
    def list_by_username(cls, db: Session, username: str, state: Optional[str] = None, mtype: Optional[str] = None):
        if mtype:
            if state:
                return db.query(cls).filter(cls.state == state,
                                            cls.username == username,
                                            cls.type == mtype).all()
            else:
                return db.query(cls).filter(cls.username == username,
                                            cls.type == mtype).all()
        else:
            if state:
                return db.query(cls).filter(cls.state == state,
                                            cls.username == username).all()
            else:
                return db.query(cls).filter(cls.username == username).all()

    @classmethod
    @async_db_query
    async def async_list_by_username(cls, db: AsyncSession, username: str, state: Optional[str] = None,
                                     mtype: Optional[str] = None):
        if mtype:
            if state:
                result = await db.execute(
                    select(cls).filter(cls.state == state, cls.username == username, cls.type == mtype)
                )
            else:
                result = await db.execute(
                    select(cls).filter(cls.username == username, cls.type == mtype)
                )
        else:
            if state:
                result = await db.execute(
                    select(cls).filter(cls.state == state, cls.username == username)
                )
            else:
                result = await db.execute(
                    select(cls).filter(cls.username == username)
                )
        return result.scalars().all()

    @classmethod
    @db_query
    def list_by_type(cls, db: Session, mtype: str, days: int):
        return db.query(cls) \
            .filter(cls.type == mtype,
                    cls.date >= time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(time.time() - 86400 * int(days)))
                    ).all()

    @classmethod
    @async_db_query
    async def async_list_by_type(cls, db: AsyncSession, mtype: str, days: int):
        result = await db.execute(
            select(cls).filter(
                cls.type == mtype,
                cls.date >= time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(time.time() - 86400 * int(days)))
            )
        )
        return result.scalars().all()
