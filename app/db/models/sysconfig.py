from sqlalchemy import Boolean, Column, Integer, Sequence
from sqlalchemy.orm import Session

from app.db import db_query, db_update, Base
from app.schemas import SysConfig


class SysConfig(Base):
    """
    配置表
    """
    # ID
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # User ID
    uid = Column(Integer, index=True)
    # 媒体统计
    mediaStatistic = Column(Boolean(), default=True)
    # 后台任务
    scheduler = Column(Boolean(), default=False)
    # 实时速率
    speed = Column(Boolean(), default=False)
    # 存储空间
    storage = Column(Boolean(), default=True)
    # 最近入库
    weeklyOverview = Column(Boolean(), default=False)
    # CPU
    cpu = Column(Boolean(), default=False)
    # 内存
    memory = Column(Boolean(), default=False)
    # 我的媒体库
    library = Column(Boolean(), default=True)
    # 继续观看
    playing = Column(Boolean(), default=True)
    # 最近添加
    latest = Column(Boolean(), default=True)

    @staticmethod
    @db_query
    def get_by_uid(db: Session, uid: int):
        return db.query(SysConfig).filter(SysConfig.uid == uid).first()

    @db_update
    def update_by_uid(self, db: Session, uid: int, **kwargs):
        config = self.get_by_uid(db, uid)
        if config:
            for key, value in kwargs.items():
                setattr(config, key, value)
        else:
            config = SysConfig(uid=uid, **kwargs)
            db.add(config)

    @db_update
    def delete_by_uid(self, db: Session, uid: int):
        config = self.get_by_uid(db, uid)
        if config:
            config.delete(db, config.id)
        return True
