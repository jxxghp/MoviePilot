from typing import Optional

from sqlalchemy import Column, Integer, String, Sequence, Float, JSON, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import db_query, Base, async_db_query


class SubscribeHistory(Base):
    """
    订阅历史表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 标题
    name = Column(String, nullable=False, index=True)
    # 年份
    year = Column(String)
    # 类型
    type = Column(String)
    # 搜索关键字
    keyword = Column(String)
    tmdbid = Column(Integer, index=True)
    imdbid = Column(String)
    tvdbid = Column(Integer)
    doubanid = Column(String, index=True)
    bangumiid = Column(Integer, index=True)
    mediaid = Column(String, index=True)
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
    # 总集数
    total_episode = Column(Integer)
    # 开始集数
    start_episode = Column(Integer)
    # 订阅完成时间
    date = Column(String)
    # 订阅用户
    username = Column(String)
    # 订阅站点
    sites = Column(JSON)
    # 是否洗版
    best_version = Column(Integer, default=0)
    # 保存路径
    save_path = Column(String)
    # 是否使用 imdbid 搜索
    search_imdbid = Column(Integer, default=0)
    # 自定义识别词
    custom_words = Column(String)
    # 自定义媒体类别
    media_category = Column(String)
    # 过滤规则组
    filter_groups = Column(JSON, default=list)
    # 剧集组
    episode_group = Column(String)

    @classmethod
    @db_query
    def list_by_type(cls, db: Session, mtype: str, page: Optional[int] = 1, count: Optional[int] = 30):
        return db.query(cls).filter(
            cls.type == mtype
        ).order_by(
            cls.date.desc()
        ).offset((page - 1) * count).limit(count).all()

    @classmethod
    @async_db_query
    async def async_list_by_type(cls, db: AsyncSession, mtype: str, page: Optional[int] = 1, count: Optional[int] = 30):
        result = await db.execute(
            select(cls).filter(
                cls.type == mtype
            ).order_by(
                cls.date.desc()
            ).offset((page - 1) * count).limit(count)
        )
        return result.scalars().all()

    @classmethod
    @db_query
    def exists(cls, db: Session, tmdbid: Optional[int] = None, doubanid: Optional[str] = None,
               season: Optional[int] = None):
        if tmdbid:
            if season:
                return db.query(cls).filter(cls.tmdbid == tmdbid,
                                            cls.season == season).first()
            return db.query(cls).filter(cls.tmdbid == tmdbid).first()
        elif doubanid:
            return db.query(cls).filter(cls.doubanid == doubanid).first()
        return None

    @classmethod
    @async_db_query
    async def async_exists(cls, db: AsyncSession, tmdbid: Optional[int] = None, doubanid: Optional[str] = None,
                           season: Optional[int] = None):
        if tmdbid:
            if season:
                result = await db.execute(
                    select(cls).filter(cls.tmdbid == tmdbid, cls.season == season)
                )
            else:
                result = await db.execute(
                    select(cls).filter(cls.tmdbid == tmdbid)
                )
        elif doubanid:
            result = await db.execute(
                select(cls).filter(cls.doubanid == doubanid)
            )
        else:
            return None
        return result.scalars().first()
