"""历史插件实例残留的受限数据库清理适配器。"""

from collections.abc import Callable

from sqlalchemy import JSON, delete, or_
from sqlalchemy.orm import Session

from app.db.models.plugininstance import PluginInstance
from app.schemas.plugin import validate_plugin_id


class PluginInstanceMaintenance:
    """只回收非法 ID 的空本体行，不接触插件文件或其它数据表。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存独占短事务工厂，构造时不访问数据库。"""
        self._session_factory = session_factory

    def purge_invalid_empty_host(self, instance_id: str) -> bool:
        """原子删除仍为空的非法本体，保留分身、启用位及任何用户设置。

        所有条件进入同一条 DELETE，避免检查后并发保存配置导致误删。JSON 列历史上
        同时存在 SQL NULL 和 JSON null；空对象仍是一份配置，不能当作无配置回收。
        ID 仅作为 SQL 参数，不参与模块、文件路径或插件自有数据库寻址。
        """
        try:
            validate_plugin_id(instance_id)
        except ValueError:
            pass
        else:
            return False
        with self._session_factory() as session, session.begin():
            result = session.execute(
                delete(PluginInstance).where(
                    PluginInstance.instance_id == instance_id,
                    PluginInstance.source_plugin_id == instance_id,
                    PluginInstance.is_enabled.is_(False),
                    PluginInstance.is_default_target.is_(False),
                    or_(PluginInstance.config_data.is_(None), PluginInstance.config_data == JSON.NULL),
                    *(
                        or_(field.is_(None), field == "")
                        for field in (
                            PluginInstance.plugin_name,
                            PluginInstance.plugin_desc,
                            PluginInstance.plugin_icon,
                            PluginInstance.log_level,
                            PluginInstance.log_expires_at,
                        )
                    ),
                ).execution_options(synchronize_session=False)
            )
            return bool(result.rowcount)
