from sqlalchemy import Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.db.models import Base


class PluginData(Base):
    """
    插件数据表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    plugin_id = Column(String, nullable=False, index=True)
    key = Column(String, index=True, nullable=False)
    value = Column(String)

    @staticmethod
    def get_plugin_data(db: Session, plugin_id: str):
        return db.query(PluginData).filter(PluginData.plugin_id == plugin_id).all()

    @staticmethod
    def get_plugin_data_by_key(db: Session, plugin_id: str, key: str):
        return db.query(PluginData).filter(PluginData.plugin_id == plugin_id, PluginData.key == key).first()

    @staticmethod
    def del_plugin_data_by_key(db: Session, plugin_id: str, key: str):
        return db.query(PluginData).filter(PluginData.plugin_id == plugin_id, PluginData.key == key).delete()
