import time
from typing import Optional

from sqlalchemy import Column, Integer, String, Float, JSON, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import db_query, db_update, get_id_column, Base, async_db_query, async_db_update


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
    username = Column(String)
    # 订阅站点
    sites = Column(JSON, default=list)
    # 下载器
    downloader = Column(String)
    # 是否洗版
    best_version = Column(Integer, default=0)
    # 当前优先级
    current_priority = Column(Integer)
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
        if season:
            return db.query(cls).filter(cls.name == title,
                                        cls.season == season).first()
        return db.query(cls).filter(cls.name == title).first()

    @classmethod
    @async_db_query
    async def async_get_by_title(cls, db: AsyncSession, title: str, season: Optional[int] = None):
        if season:
            result = await db.execute(
                select(cls).filter(cls.name == title, cls.season == season)
            )
        else:
            result = await db.execute(
                select(cls).filter(cls.name == title)
            )
        return result.scalars().first()

    @classmethod
    @db_query
    def get_by_tmdbid(cls, db: Session, tmdbid: int, season: Optional[int] = None):
        if season:
            return db.query(cls).filter(cls.tmdbid == tmdbid,
                                        cls.season == season).all()
        else:
            return db.query(cls).filter(cls.tmdbid == tmdbid).all()

    @classmethod
    @async_db_query
    async def async_get_by_tmdbid(cls, db: AsyncSession, tmdbid: int, season: Optional[int] = None):
        if season:
            result = await db.execute(
                select(cls).filter(cls.tmdbid == tmdbid, cls.season == season)
            )
        else:
            result = await db.execute(
                select(cls).filter(cls.tmdbid == tmdbid)
            )
        return result.scalars().all()

    @classmethod
    @db_query
    def get_by_doubanid(cls, db: Session, doubanid: str):
        return db.query(cls).filter(cls.doubanid == doubanid).first()

    @classmethod
    @async_db_query
    async def async_get_by_doubanid(cls, db: AsyncSession, doubanid: str):
        result = await db.execute(
            select(cls).filter(cls.doubanid == doubanid)
        )
        return result.scalars().first()

    @classmethod
    @db_query
    def get_by_bangumiid(cls, db: Session, bangumiid: int):
        return db.query(cls).filter(cls.bangumiid == bangumiid).first()

    @classmethod
    @async_db_query
    async def async_get_by_bangumiid(cls, db: AsyncSession, bangumiid: int):
        result = await db.execute(
            select(cls).filter(cls.bangumiid == bangumiid)
        )
        return result.scalars().first()

    @classmethod
    @db_query
    def get_by_mediaid(cls, db: Session, mediaid: str):
        return db.query(cls).filter(cls.mediaid == mediaid).first()

    @classmethod
    @async_db_query
    async def async_get_by_mediaid(cls, db: AsyncSession, mediaid: str):
        result = await db.execute(
            select(cls).filter(cls.mediaid == mediaid)
        )
        return result.scalars().first()

    @db_update
    def delete_by_tmdbid(self, db: Session, tmdbid: int, season: int):
        subscrbies = self.get_by_tmdbid(db, tmdbid, season)
        for subscrbie in subscrbies:
            subscrbie.delete(db, subscrbie.id)
        return True

    @async_db_update
    async def async_delete_by_tmdbid(self, db: AsyncSession, tmdbid: int, season: int):
        subscrbies = await self.async_get_by_tmdbid(db, tmdbid, season)
        for subscrbie in subscrbies:
            await subscrbie.async_delete(db, subscrbie.id)
        return True

    @db_update
    def delete_by_doubanid(self, db: Session, doubanid: str):
        subscribe = self.get_by_doubanid(db, doubanid)
        if subscribe:
            subscribe.delete(db, subscribe.id)
        return True

    @async_db_update
    async def async_delete_by_doubanid(self, db: AsyncSession, doubanid: str):
        subscribe = await self.async_get_by_doubanid(db, doubanid)
        if subscribe:
            await subscribe.async_delete(db, subscribe.id)
        return True

    @db_update
    def delete_by_mediaid(self, db: Session, mediaid: str):
        subscribe = self.get_by_mediaid(db, mediaid)
        if subscribe:
            subscribe.delete(db, subscribe.id)
        return True

    @async_db_update
    async def async_delete_by_mediaid(self, db: AsyncSession, mediaid: str):
        subscribe = await self.async_get_by_mediaid(db, mediaid)
        if subscribe:
            await subscribe.async_delete(db, subscribe.id)
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
