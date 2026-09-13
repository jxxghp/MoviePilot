"""插件运行时持久化端口。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, NamedTuple, Optional

from pydantic import ValidationError

from app.runtime.extensions.plugin.database import PluginDatabase
from app.schemas.plugin import PluginInstance
from app.schemas.types import SystemConfigKey


ConfigReader = Callable[[Any], Any]
ConfigWriter = Callable[[Any, Any], Any]
AsyncConfigWriter = Callable[[Any, Any], Awaitable[Any]]
ConfigDeleter = Callable[[Any], bool]
# 插件配置端口按实例 ID 寻址，不经过任何字符串键：一个实例的业务参数存在它自己
# 那一行上，宿主的系统设置存储不必知道插件存在
InstanceConfigReader = Callable[[str], Any]
InstanceConfigWriter = Callable[[str, Any], Any]
AsyncInstanceConfigWriter = Callable[[str, Any], Awaitable[Any]]
InstanceConfigDeleter = Callable[[str], bool]
PluginDataDeleter = Callable[[str], Any]
# 日志等级覆盖端口的载荷：`(等级名, 失效时间)`，两者皆为 None 即未设置覆盖
LogLevelOverride = tuple[Optional[str], Optional[datetime]]
LogLevelReader = Callable[[str], LogLevelOverride]
LogLevelWriter = Callable[[str, Optional[str], Optional[datetime]], None]
PluginExists = Callable[[str], bool]


def _empty_read(_key: Any) -> Any:
    """组合根尚未装配时返回空配置。"""
    return None


def _ignore_write(_key: Any, _value: Any) -> None:
    """组合根尚未装配时忽略同步配置写入。"""


async def _ignore_async_write(_key: Any, _value: Any) -> None:
    """组合根尚未装配时忽略异步配置写入。"""


def _ignore_delete(_key: Any) -> bool:
    """组合根尚未装配时报告配置未删除。"""
    return False


def _empty_read_config(_instance_id: str) -> Any:
    """组合根尚未装配时报告实例没有业务参数。"""
    return None


def _ignore_write_config(_instance_id: str, _config: Any) -> None:
    """组合根尚未装配时忽略同步业务参数写入。"""


async def _ignore_async_write_config(_instance_id: str, _config: Any) -> None:
    """组合根尚未装配时忽略异步业务参数写入。"""


def _ignore_delete_config(_instance_id: str) -> bool:
    """组合根尚未装配时报告业务参数未删除。"""
    return False


def _ignore_plugin_data_delete(_plugin_id: str) -> None:
    """组合根尚未装配时忽略插件数据删除。"""


def _no_log_level(_instance_id: str) -> LogLevelOverride:
    """组合根尚未装配时报告实例没有日志等级覆盖。"""
    return (None, None)


def _ignore_log_level_write(
    _instance_id: str,
    _level: Optional[str],
    _expires_at: Optional[datetime],
) -> None:
    """组合根尚未装配时忽略日志等级落盘。"""


class PluginStorage:
    """封装插件运行时所需的最小持久化能力。"""

    def __init__(
            self,
            *,
            read: ConfigReader = _empty_read,
            write: ConfigWriter = _ignore_write,
            async_write: AsyncConfigWriter = _ignore_async_write,
            delete: ConfigDeleter = _ignore_delete,
            read_config: InstanceConfigReader = _empty_read_config,
            write_config: InstanceConfigWriter = _ignore_write_config,
            async_write_config: AsyncInstanceConfigWriter = _ignore_async_write_config,
            delete_config: InstanceConfigDeleter = _ignore_delete_config,
            delete_data: PluginDataDeleter = _ignore_plugin_data_delete,
            read_log_level: LogLevelReader = _no_log_level,
            write_log_level: LogLevelWriter = _ignore_log_level_write,
    ) -> None:
        """保存由启动组合根提供的读写函数。"""
        self._read = read
        self._write = write
        self._async_write = async_write
        self._delete = delete
        self._read_config = read_config
        self._write_config = write_config
        self._async_write_config = async_write_config
        self._delete_config = delete_config
        self._delete_data = delete_data
        self._read_log_level = read_log_level
        self._write_log_level = write_log_level

    def read(self, key: Any) -> Any:
        """读取插件运行时配置。"""
        return self._read(key)

    def write(self, key: Any, value: Any) -> Any:
        """同步保存插件运行时配置。"""
        return self._write(key, value)

    async def async_write(self, key: Any, value: Any) -> Any:
        """异步保存插件运行时配置。"""
        return await self._async_write(key, value)

    def delete(self, key: Any) -> bool:
        """删除插件运行时配置。"""
        return self._delete(key)

    def read_config(self, instance_id: str) -> Any:
        """读取该实例配置行上的业务参数。"""
        return self._read_config(instance_id)

    def write_config(self, instance_id: str, config: Any) -> Any:
        """同步把业务参数写进该实例的配置行。"""
        return self._write_config(instance_id, config)

    async def async_write_config(self, instance_id: str, config: Any) -> Any:
        """异步把业务参数写进该实例的配置行。"""
        return await self._async_write_config(instance_id, config)

    def delete_config(self, instance_id: str) -> bool:
        """清除该实例配置行上的业务参数。"""
        return self._delete_config(instance_id)

    def delete_data(self, plugin_id: str) -> Any:
        """删除指定插件的业务数据。"""
        return self._delete_data(plugin_id)

    def read_log_level(self, instance_id: str) -> LogLevelOverride:
        """读取实例配置里登记的日志等级覆盖与失效时间。"""
        return self._read_log_level(instance_id)

    def write_log_level(
        self,
        instance_id: str,
        level: Optional[str],
        expires_at: Optional[datetime],
    ) -> None:
        """把日志等级覆盖写进该实例的配置行。"""
        self._write_log_level(instance_id, level, expires_at)


class PluginConfigStore:
    """封装插件配置的存在性和强制删除规则。

    配置按实例 ID 直接落在该实例自己的配置行上，不再经过宿主系统设置存储的字符串键：
    插件配置与系统设置的生命周期、数量级和归属都不同，共用一张键值表会让系统设置被
    插件条目淹没，也会让「某插件的全部实例配置」只能靠键前缀去猜。
    """

    def __init__(
        self,
        *,
        storage: Callable[[], "PluginStorage"],
        database: Callable[[], PluginDatabase],
        plugin_exists: PluginExists,
    ) -> None:
        """保存持久化端口、自有数据库端口和运行态插件查询端口。"""
        self._storage = storage
        self._database = database
        self._plugin_exists = plugin_exists

    def read(self, plugin_id: str) -> dict:
        """读取配置并过滤历史空键。"""
        if not self._plugin_exists(plugin_id):
            return {}
        config = self._storage().read_config(plugin_id)
        return {
            key: value
            for key, value in (config or {}).items()
            if key
        }

    def write(self, plugin_id: str, config: dict, force: bool = False) -> bool:
        """保存配置，默认拒绝不存在插件的配置写入。"""
        if not force and not self._plugin_exists(plugin_id):
            return False
        self._storage().write_config(plugin_id, config)
        return True

    async def async_write(
        self,
        plugin_id: str,
        config: dict,
        force: bool = False,
    ) -> bool:
        """异步保存配置并保持同步写入的存在性规则。"""
        if not force and not self._plugin_exists(plugin_id):
            return False
        await self._storage().async_write_config(plugin_id, config)
        return True

    def delete(self, plugin_id: str, force: bool = False) -> bool:
        """删除配置并保持停止插件后的强制删除能力。"""
        if not force and not self._plugin_exists(plugin_id):
            return False
        return self._storage().delete_config(plugin_id)

    def read_log_level(self, instance_id: str) -> LogLevelOverride:
        """读取实例的日志等级覆盖；它与业务参数同属该实例的配置。"""
        return self._storage().read_log_level(instance_id)

    def write_log_level(
        self,
        instance_id: str,
        level: Optional[str],
        expires_at: Optional[datetime],
    ) -> None:
        """写入实例的日志等级覆盖。"""
        self._storage().write_log_level(instance_id, level, expires_at)

    def delete_data(self, plugin_id: str, force: bool = False) -> bool:
        """删除插件业务数据与自有数据库，并保持旧的布尔结果合同。"""
        if not force and not self._plugin_exists(plugin_id):
            return False
        self._storage().delete_data(plugin_id)
        self._database().destroy(plugin_id)
        return True


InstanceReader = Callable[[str], "PluginInstance | None"]
InstanceLister = Callable[[], "list[PluginInstance]"]
InstanceSourceLister = Callable[[str], "list[PluginInstance]"]
InstanceWriter = Callable[["PluginInstance"], None]
InstanceDeleter = Callable[[str], bool]


def _empty_instance_get(_instance_id: str) -> PluginInstance | None:
    """组合根尚未装配时返回空实例描述。"""
    return None


def _empty_instance_list() -> list[PluginInstance]:
    """组合根尚未装配时返回空实例列表。"""
    return []


def _empty_instance_list_by_source(_source_plugin_id: str) -> list[PluginInstance]:
    """组合根尚未装配时返回空实例列表。"""
    return []


def _ignore_instance_save(_instance: PluginInstance) -> None:
    """组合根尚未装配时忽略实例描述写入。"""


def _ignore_instance_delete(_instance_id: str) -> bool:
    """组合根尚未装配时报告实例描述未删除。"""
    return False


class PluginInstanceDirectory:
    """封装插件实例表的持久化能力。

    分身与源插件本体共用同一张表、同一套读写原语，两者靠 ``instance_id`` 是否等于
    ``source_plugin_id`` 区分；本类不做角色过滤，角色隔离由调用方
    （``PluginInstanceStore``）负责，因为只有调用方知道当前服务的是分身清单还是
    本体自身的那一行。
    """

    def __init__(
            self,
            *,
            get: InstanceReader = _empty_instance_get,
            list_all: InstanceLister = _empty_instance_list,
            list_by_source: InstanceSourceLister = _empty_instance_list_by_source,
            save: InstanceWriter = _ignore_instance_save,
            delete: InstanceDeleter = _ignore_instance_delete,
    ) -> None:
        """保存由启动组合根提供的实例表读写函数。"""
        self._get = get
        self._list_all = list_all
        self._list_by_source = list_by_source
        self._save = save
        self._delete = delete

    def get(self, instance_id: str) -> PluginInstance | None:
        """按实例 ID 读取单条描述，不区分分身与本体。"""
        return self._get(instance_id)

    def list_all(self) -> list[PluginInstance]:
        """列出表中全部描述，不区分分身与本体。"""
        return self._list_all()

    def list_by_source(self, source_plugin_id: str) -> list[PluginInstance]:
        """按源插件 ID 列出其全部描述，不区分分身与本体。"""
        return self._list_by_source(source_plugin_id)

    def save(self, instance: PluginInstance) -> None:
        """新增或更新一条描述，以 ``instance_id`` 为稳定键。"""
        self._save(instance)

    def delete(self, instance_id: str) -> bool:
        """按实例 ID 删除一行，连同其配置，返回删除前是否存在。"""
        return self._delete(instance_id)


class _LegacyInstanceEntry(NamedTuple):
    """旧 systemconfig 单键里的一条实例描述，连同其原始载荷的内容指纹。"""

    instance: PluginInstance
    fingerprint: str


def _payload_fingerprint(payload: dict[str, Any]) -> str:
    """为旧键里的一条原始载荷算内容指纹，用于判定旧版本此后有没有改动过它。

    指纹取自原始载荷而非模型 dump：模型字段将来增减会让全部指纹一起失配，把用户在
    独立表里的改动当成「旧载荷变化」整批盖回去。键排序后再序列化，旧版本重写整个键
    造成的字段顺序变化不会被误判成内容变化。
    :param payload: 旧键里的单条原始载荷
    :return: 十六进制摘要
    """
    canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PluginInstanceStore:
    """管理共享源码的分身实例描述，并把源插件本体自身的那一行隔离在视图之外。

    两类记录同存一张表，靠 ``instance_id`` 是否等于 ``source_plugin_id`` 区分：
    本类只服务分身，本体行（它承载插件自身的业务参数）读不到也改不到。
    """

    def __init__(
            self,
            *,
            storage: Callable[[], "PluginStorage"],
            directory: Callable[[], PluginInstanceDirectory],
    ) -> None:
        """保存独立表持久化端口，以及旧 systemconfig 单键端口供兜底导入使用。"""
        self._storage = storage
        self._directory = directory
        self._bootstrap_checked = False

    def _ensure_bootstrapped(self) -> None:
        """把旧 systemconfig 单键里新增或变化的实例描述合并进独立表，每进程判定一次。

        alembic 迁移已经搬过一轮，这里兜的是「库结构升级后又用旧版本写过分身」这类
        回滚往返：旧键刻意保留作回滚依据、从不清理，旧版本只认它，因此它在迁移之后
        仍可能长出新条目或被改写。判据既不能是「表当前为空」——用户删光分身后表会重新
        变空，下次启动会把删掉的分身整批导回来；也不能是一条一次性的永久标记——标记
        落下之后旧版本新增的分身就永远看不见。

        因此按条目记住导入时的内容指纹：指纹对得上就跳过，表里那行用户怎么改都不会被
        旧副本盖回去；指纹对不上说明旧版本改写过该条目，合并进来；指纹没记过且表里也
        没有该行说明旧版本新增过它，导入。指纹没记过但表里已有该行（迁移刚搬过去的那
        一批），只认领指纹而不覆盖。指纹表始终与旧键当前内容对齐，重复启动不再产生写入。

        :raise Exception: 导入失败时向上抛出，不吞掉持久化层错误
        """
        if self._bootstrap_checked:
            return
        self._bootstrap_checked = True
        storage = self._storage()
        recorded = storage.read(SystemConfigKey.PluginInstancesImported)
        imported: dict[str, str] = dict(recorded) if isinstance(recorded, dict) else {}
        legacy = self._legacy_instances()
        pending = {
            instance_id: entry
            for instance_id, entry in legacy.items()
            if imported.get(instance_id) != entry.fingerprint
        }
        if pending:
            directory = self._directory()
            # 只在确有待定条目时扫一次现有行，让无变化的启动不额外查表
            present = {record.instance_id for record in directory.list_all()}
            for instance_id, entry in pending.items():
                if instance_id in imported or instance_id not in present:
                    directory.save(entry.instance)
        fingerprints = {
            instance_id: entry.fingerprint for instance_id, entry in legacy.items()
        }
        if fingerprints != imported:
            storage.write(SystemConfigKey.PluginInstancesImported, fingerprints)

    def _legacy_instances(self) -> dict[str, _LegacyInstanceEntry]:
        """解析旧 systemconfig 单键里的实例描述，兼容历史字典与列表两种载荷形态。"""
        raw_instances = self._storage().read(SystemConfigKey.PluginInstances) or {}
        if isinstance(raw_instances, list):
            entries = {
                item.get("instance_id"): item
                for item in raw_instances
                if isinstance(item, dict) and item.get("instance_id")
            }
        elif isinstance(raw_instances, dict):
            entries = raw_instances
        else:
            return {}

        instances: dict[str, _LegacyInstanceEntry] = {}
        for instance_id, raw_instance in entries.items():
            try:
                payload = dict(raw_instance) if isinstance(raw_instance, dict) else {}
                payload.setdefault("instance_id", instance_id)
                instance = PluginInstance.model_validate(payload)
                instances[instance.instance_id] = _LegacyInstanceEntry(
                    instance=instance,
                    fingerprint=_payload_fingerprint(payload),
                )
            except (TypeError, ValidationError):
                continue
        return instances

    def all(self) -> dict[str, PluginInstance]:
        """读取全部登记的分身实例，不含源插件本体自身的那一行。"""
        self._ensure_bootstrapped()
        return {
            record.instance_id: record
            for record in self._directory().list_all()
            if not record.is_host
        }

    def get(self, instance_id: str) -> PluginInstance | None:
        """读取指定分身实例描述；本体自身的那一行不会从这里返回。"""
        self._ensure_bootstrapped()
        record = self._directory().get(instance_id)
        return record if record is not None and not record.is_host else None

    def save(self, instance: PluginInstance) -> None:
        """新增或更新分身实例描述，并以实例 ID 作为稳定持久化键。

        分身的实例 ID 必须区别于其源插件 ID：两者相等的那一行表示的是本体自身，
        按分身写入会把本体承载的业务参数顶掉。
        """
        self._ensure_bootstrapped()
        if instance.is_host:
            raise ValueError(
                f"分身实例 {instance.instance_id} 的 ID 不能等于其源插件 ID"
            )
        self._directory().save(instance)

    def delete(self, instance_id: str) -> bool:
        """删除指定分身实例描述连同其配置，返回删除前是否存在。"""
        self._ensure_bootstrapped()
        record = self.get(instance_id)
        if record is None:
            return False
        return self._directory().delete(record.instance_id)

    def for_source(self, source_plugin_id: str) -> list[PluginInstance]:
        """按持久化顺序返回引用同一源码插件的全部分身，不含本体自身那一行。"""
        self._ensure_bootstrapped()
        return [
            record
            for record in self._directory().list_by_source(source_plugin_id)
            if not record.is_host
        ]


_plugin_storage = PluginStorage()
_plugin_instance_directory = PluginInstanceDirectory()


def configure_plugin_instance_directory(directory: PluginInstanceDirectory) -> None:
    """由启动组合根替换插件实例表持久化实现。"""
    global _plugin_instance_directory
    _plugin_instance_directory = directory


def get_plugin_instance_directory() -> PluginInstanceDirectory:
    """返回当前插件实例表持久化端口。"""
    return _plugin_instance_directory


def configure_plugin_storage(storage: PluginStorage) -> None:
    """由启动组合根替换插件运行时持久化实现。"""
    global _plugin_storage
    _plugin_storage = storage


def get_plugin_storage() -> PluginStorage:
    """返回当前插件运行时持久化端口。"""
    return _plugin_storage
