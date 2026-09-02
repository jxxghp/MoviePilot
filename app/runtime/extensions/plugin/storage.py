"""插件运行时持久化端口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from app.runtime.extensions.plugin.database import PluginDatabase
from app.schemas.plugin import PluginInstance
from app.schemas.types import SystemConfigKey


ConfigReader = Callable[[Any], Any]
ConfigWriter = Callable[[Any, Any], Any]
AsyncConfigWriter = Callable[[Any, Any], Awaitable[Any]]
ConfigDeleter = Callable[[Any], bool]
PluginDataDeleter = Callable[[str], Any]
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


def _ignore_plugin_data_delete(_plugin_id: str) -> None:
    """组合根尚未装配时忽略插件数据删除。"""


class PluginStorage:
    """封装插件运行时所需的最小持久化能力。"""

    def __init__(
            self,
            *,
            read: ConfigReader = _empty_read,
            write: ConfigWriter = _ignore_write,
            async_write: AsyncConfigWriter = _ignore_async_write,
            delete: ConfigDeleter = _ignore_delete,
            delete_data: PluginDataDeleter = _ignore_plugin_data_delete,
    ) -> None:
        """保存由启动组合根提供的读写函数。"""
        self._read = read
        self._write = write
        self._async_write = async_write
        self._delete = delete
        self._delete_data = delete_data

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

    def delete_data(self, plugin_id: str) -> Any:
        """删除指定插件的业务数据。"""
        return self._delete_data(plugin_id)


class PluginConfigStore:
    """封装插件配置键、存在性和强制删除规则。"""

    def __init__(
        self,
        *,
        storage: Callable[[], "PluginStorage"],
        database: Callable[[], PluginDatabase],
        plugin_exists: PluginExists,
        key_prefix: str = "plugin.%s",
    ) -> None:
        """保存持久化端口、自有数据库端口和运行态插件查询端口。"""
        self._storage = storage
        self._database = database
        self._plugin_exists = plugin_exists
        self._key_prefix = key_prefix

    def _key(self, plugin_id: str) -> str:
        """构造插件配置在统一配置存储中的键。"""
        return self._key_prefix % plugin_id

    def read(self, plugin_id: str) -> dict:
        """读取配置并过滤历史空键。"""
        if not self._plugin_exists(plugin_id):
            return {}
        config = self._storage().read(self._key(plugin_id))
        return {
            key: value
            for key, value in (config or {}).items()
            if key
        }

    def write(self, plugin_id: str, config: dict, force: bool = False) -> bool:
        """保存配置，默认拒绝不存在插件的配置写入。"""
        if not force and not self._plugin_exists(plugin_id):
            return False
        self._storage().write(self._key(plugin_id), config)
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
        await self._storage().async_write(self._key(plugin_id), config)
        return True

    def delete(self, plugin_id: str, force: bool = False) -> bool:
        """删除配置并保持停止插件后的强制删除能力。"""
        if not force and not self._plugin_exists(plugin_id):
            return False
        return self._storage().delete(self._key(plugin_id))

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
    """封装插件实例描述符独立表的持久化能力。

    分身与源插件本体的版本绑定共用同一张表、同一套读写原语，两者只靠各自
    ``PluginInstance.mode`` 取值区分；本类不做角色过滤，角色隔离由调用方
    （``PluginInstanceStore``）负责，因为只有调用方知道当前是在服务分身清单
    还是本体绑定这两类完全不同的语义。
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
        """保存由启动组合根提供的实例描述符表读写函数。"""
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
        """按实例 ID 删除一条描述，返回删除前是否存在。"""
        return self._delete(instance_id)


_plugin_instance_directory = PluginInstanceDirectory()


def configure_plugin_instance_directory(directory: PluginInstanceDirectory) -> None:
    """由启动组合根替换插件实例描述符表持久化实现。"""
    global _plugin_instance_directory
    _plugin_instance_directory = directory


def get_plugin_instance_directory() -> PluginInstanceDirectory:
    """返回当前插件实例描述符表持久化端口。"""
    return _plugin_instance_directory


class PluginInstanceStore:
    """管理虚拟插件实例描述与源插件本体的版本绑定，二者互不进入对方视图。

    两类记录同存一张独立表，只靠 ``mode`` 字段区分：``all()``/``get()``/
    ``save()``/``delete()``/``for_source()`` 只服务分身，是这些方法迁移前
    的既有合同；``get_host()``/``save_host()`` 是本任务新增的本体访问入口，
    只服务本体。任何一侧都读不到、也删不到对方的记录。
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
        """新表为空而旧 systemconfig 单键非空时，把旧内容原样导入表一次。

        触发条件是「表当前为空」这一实测事实，不是某个一次性开关：导入完成后
        表不再为空，同一份数据不会被重复导入；进程内额外维护一个已检查标志，
        避免每次访问都为判空多打一次查询。

        :raise Exception: 导入失败时向上抛出，不吞掉持久化层错误
        """
        if self._bootstrap_checked:
            return
        self._bootstrap_checked = True
        directory = self._directory()
        if directory.list_all():
            return
        for instance in self._legacy_instances().values():
            directory.save(instance)

    def _legacy_instances(self) -> dict[str, PluginInstance]:
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

        instances: dict[str, PluginInstance] = {}
        for instance_id, raw_instance in entries.items():
            try:
                payload = dict(raw_instance) if isinstance(raw_instance, dict) else {}
                payload.setdefault("instance_id", instance_id)
                instance = PluginInstance.model_validate(payload)
                instances[instance.instance_id] = instance
            except (TypeError, ValidationError):
                continue
        return instances

    def all(self) -> dict[str, PluginInstance]:
        """读取全部有效分身实例，不含源插件本体的版本绑定记录。"""
        self._ensure_bootstrapped()
        return {
            record.instance_id: record
            for record in self._directory().list_all()
            if record.mode == "virtual"
        }

    def get(self, instance_id: str) -> PluginInstance | None:
        """读取指定分身实例描述，本体的版本绑定记录不会从这里返回。"""
        self._ensure_bootstrapped()
        record = self._directory().get(instance_id)
        return record if record is not None and record.mode == "virtual" else None

    def save(self, instance: PluginInstance) -> None:
        """新增或更新分身实例描述，并以实例 ID 作为稳定持久化键。"""
        self._ensure_bootstrapped()
        self._directory().save(instance.model_copy(update={"mode": "virtual"}))

    def delete(self, instance_id: str) -> bool:
        """删除指定分身实例描述，返回删除前是否存在。"""
        self._ensure_bootstrapped()
        if self.get(instance_id) is None:
            return False
        return self._directory().delete(instance_id)

    def for_source(self, source_plugin_id: str) -> list[PluginInstance]:
        """按持久化顺序返回引用同一源插件的全部分身实例，不含本体绑定记录。"""
        self._ensure_bootstrapped()
        return [
            record
            for record in self._directory().list_by_source(source_plugin_id)
            if record.mode == "virtual"
        ]

    def get_host(self, plugin_id: str) -> PluginInstance | None:
        """读取源插件本体的版本绑定记录；从未显式绑定过版本时为 None。"""
        self._ensure_bootstrapped()
        record = self._directory().get(plugin_id)
        return record if record is not None and record.mode == "host" else None

    def save_host(self, instance: PluginInstance) -> None:
        """新增或更新源插件本体的版本绑定记录，本体的 ``instance_id`` 恒等于其自身 ID。"""
        self._ensure_bootstrapped()
        self._directory().save(
            instance.model_copy(
                update={"mode": "host", "source_plugin_id": instance.instance_id}
            )
        )

    def record_effective_version(self, instance_id: str, version: str) -> None:
        """把本次成功启动所用的版本登记为分身或本体的已生效版本。

        分身尚未创建、本体从未被显式绑定过版本时都读取为空，此时静默跳过而不
        是隐式创建一条记录，避免每个物理插件的每次成功启动都触发一次持久化
        写入；值未变化时同样不产生写入。

        :param instance_id: 实例 ID，也可能是分身与本体都未持有记录的物理插件 ID
        :param version: 本次成功加载的源码所声明的版本号
        """
        instance = self.get(instance_id)
        if instance is not None:
            if instance.plugin_version != version:
                self.save(instance.model_copy(update={"plugin_version": version}))
            return
        host_instance = self.get_host(instance_id)
        if host_instance is not None and host_instance.plugin_version != version:
            self.save_host(host_instance.model_copy(update={"plugin_version": version}))


_plugin_storage = PluginStorage()


def configure_plugin_storage(storage: PluginStorage) -> None:
    """由启动组合根替换插件运行时持久化实现。"""
    global _plugin_storage
    _plugin_storage = storage


def get_plugin_storage() -> PluginStorage:
    """返回当前插件运行时持久化端口。"""
    return _plugin_storage
