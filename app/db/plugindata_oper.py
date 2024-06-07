import json
from typing import Any

from app.db import DbOper
from app.db.models.plugindata import PluginData
from app.utils.object import ObjectUtils


class PluginDataOper(DbOper):
    """
    插件数据管理
    """

    def save(self, plugin_id: str, key: str, value: Any):
        """
        保存插件数据
        :param plugin_id: 插件id
        :param key: 数据key
        :param value: 数据值
        """
        if ObjectUtils.is_obj(value):
            value = json.dumps(value)
        plugin = PluginData.get_plugin_data_by_key(self._db, plugin_id, key)
        if plugin:
            plugin.update(self._db, {
                "value": value
            })
        else:
            PluginData(plugin_id=plugin_id, key=key, value=value).create(self._db)

    def get_data(self, plugin_id: str, key: str = None) -> Any:
        """
        获取插件数据
        :param plugin_id: 插件id
        :param key: 数据key
        """
        if key:
            data = PluginData.get_plugin_data_by_key(self._db, plugin_id, key)
            if not data:
                return None
            if ObjectUtils.is_obj(data.value):
                return json.loads(data.value)
            return data.value
        else:
            return PluginData.get_plugin_data(self._db, plugin_id)

    def del_data(self, plugin_id: str, key: str = None) -> Any:
        """
        删除插件数据
        :param plugin_id: 插件id
        :param key: 数据key
        """
        if key:
            PluginData.del_plugin_data_by_key(self._db, plugin_id, key)
        else:
            PluginData.del_plugin_data(self._db, plugin_id)

    def truncate(self):
        """
        清空插件数据
        """
        PluginData.truncate(self._db)

    def get_data_all(self, plugin_id: str) -> Any:
        """
        获取插件所有数据
        :param plugin_id: 插件id
        """
        return PluginData.get_plugin_data_by_plugin_id(self._db, plugin_id)
