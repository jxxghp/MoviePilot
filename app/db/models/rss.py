from sqlalchemy import Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.db.models import Base


class Rss(Base):
    """
    RSS订阅
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 名称
    name = Column(String, nullable=False)
    # RSS地址
    url = Column(String, nullable=False)
    # 类型
    type = Column(String)
    # 标题
    title = Column(String)
    # 年份
    year = Column(String)
    # TMDBID
    tmdbid = Column(Integer, index=True)
    # 季号
    season = Column(Integer)
    # 海报
    poster = Column(String)
    # 背景图
    backdrop = Column(String)
    # 评分
    vote = Column(Integer)
    # 简介
    description = Column(String)
    # 总集数
    total_episode = Column(Integer)
    # 包含
    include = Column(String)
    # 排除
    exclude = Column(String)
    # 洗版
    best_version = Column(Integer)
    # 是否使用代理服务器
    proxy = Column(Integer)
    # 是否使用过滤规则
    filter = Column(Integer)
    # 保存路径
    save_path = Column(String)
    # 已处理数量
    processed = Column(Integer)
    # 附加信息，已处理数据
    note = Column(String)
    # 最后更新时间
    last_update = Column(String)
    # 状态 0-停用，1-启用
    state = Column(Integer, default=1)

    @staticmethod
    def get_by_tmdbid(db: Session, tmdbid: int, season: int = None):
        if season:
            return db.query(Rss).filter(Rss.tmdbid == tmdbid,
                                        Rss.season == season).all()
        return db.query(Rss).filter(Rss.tmdbid == tmdbid).all()

    @staticmethod
    def get_by_title(db: Session, title: str):
        return db.query(Rss).filter(Rss.title == title).first()
