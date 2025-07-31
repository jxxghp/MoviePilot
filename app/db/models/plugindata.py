from sqlalchemy import Column, Integer, String, Sequence, JSON
from sqlalchemy.orm import Session

from app.db import db_query, db_update, Base


class PluginData(Base):
    """
    插件数据表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    plugin_id = Column(String, nullable=False, index=True)
    key = Column(String, index=True, nullable=False)
    value = Column(JSON)

    @classmethod
    @db_query
    def get_plugin_data(cls, db: Session, plugin_id: str):
        return db.query(cls).filter(cls.plugin_id == plugin_id).all()

    @classmethod
    @db_query
    def get_plugin_data_by_key(cls, db: Session, plugin_id: str, key: str):
        return db.query(cls).filter(cls.plugin_id == plugin_id, cls.key == key).first()

    @classmethod
    @db_update
    def del_plugin_data_by_key(cls, db: Session, plugin_id: str, key: str):
        db.query(cls).filter(cls.plugin_id == plugin_id, cls.key == key).delete()

    @classmethod
    @db_update
    def del_plugin_data(cls, db: Session, plugin_id: str):
        db.query(cls).filter(cls.plugin_id == plugin_id).delete()

    @classmethod
    @db_query
    def get_plugin_data_by_plugin_id(cls, db: Session, plugin_id: str):
        return db.query(cls).filter(cls.plugin_id == plugin_id).all()
