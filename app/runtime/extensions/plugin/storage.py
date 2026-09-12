"""插件运行时持久化端口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Optional

from pydantic import ValidationError

from app.runtime.extensions.plugin.database import PluginDatabase
from app.schemas.plugin import PluginInstance
from app.schemas.types import SystemConfigKey

ConfigReader = Callable[[Any], Any]
ConfigWriter = Callable[[Any, Any], Any]
AsyncConfigWriter = Callable[[Any, Any], Awaitable[Any]]
ConfigDeleter = Callable[[Any], bool]
PluginDataDeleter = Callable[[str], Any]
PluginDataProbe = Callable[[str], bool]
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


def _ignore_plugin_data_delete(_plugin_id: str) -> None:
    """组合根尚未装配时忽略插件数据删除。"""


def _no_plugin_data(_plugin_id: str) -> bool:
    """组合根尚未装配时报告插件没有业务数据。"""
    return False


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
            delete_data: PluginDataDeleter = _ignore_plugin_data_delete,
            has_data: PluginDataProbe = _no_plugin_data,
            read_log_level: LogLevelReader = _no_log_level,
            write_log_level: LogLevelWriter = _ignore_log_level_write,
    ) -> None:
        """保存由启动组合根提供的读写函数。"""
        self._read = read
        self._write = write
        self._async_write = async_write
        self._delete = delete
        self._delete_data = delete_data
        self._has_data = has_data
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

    def delete_data(self, plugin_id: str) -> Any:
        """删除指定插件的业务数据。"""
        return self._delete_data(plugin_id)

    def has_data(self, plugin_id: str) -> bool:
        """判断指定插件是否留有业务数据。"""
        return bool(self._has_data(plugin_id))

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

    def has_data(self, plugin_id: str) -> bool:
        """判断指定插件是否留有业务数据，供同后缀重建时判定有无可恢复的残留。"""
        return self._storage().has_data(plugin_id)

    def has_config(self, plugin_id: str) -> bool:
        """判断该实例是否留有业务参数，不要求它此刻有运行态类。

        :meth:`read` 带着「插件必须已注册」的门槛，而已卸载的实例正因为不被装载才
        没有类；用它去问停用实例有没有配置，答案恒为否。
        """
        return bool(self._storage().read(self._key(plugin_id)))

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
        self.delete_data_rows(plugin_id)
        self.destroy_database(plugin_id)
        return True

    def delete_data_rows(self, plugin_id: str) -> None:
        """只删该实例在插件数据表里的行，不碰它的自有数据库。

        彻底清理要让用户逐项勾选删除范围，因而这两件事必须能分开做；
        :meth:`delete_data` 仍把它们捆在一起，服务重置那条既有语义。
        """
        self._storage().delete_data(plugin_id)

    def destroy_database(self, plugin_id: str) -> None:
        """销毁该实例的自有数据库并释放句柄。"""
        self._database().destroy(plugin_id)


InstanceReader = Callable[[str], "PluginInstance | None"]
InstanceLister = Callable[[], "list[PluginInstance]"]
InstanceSourceLister = Callable[[str], "list[PluginInstance]"]
InstanceWriter = Callable[["PluginInstance"], None]
InstanceDeleter = Callable[[str], bool]
InstanceEnabler = Callable[[str, bool], bool]


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


def _ignore_instance_enable(_instance_id: str, _is_enabled: bool) -> bool:
    """组合根尚未装配时报告启用位未写入。"""
    return False


class PluginInstanceDirectory:
    """封装插件实例表的持久化能力。

    分身与源插件本体共用同一张表、同一套读写原语，两者靠 ``instance_id`` 是否等于
    ``source_plugin_id`` 区分；本类不做角色过滤，角色隔离由调用方
    （``PluginInstanceStore``）负责，因为只有调用方知道当前是在服务分身清单
    还是本体绑定这两类完全不同的语义。

    枚举口一律返回全部登记行（含停用的），``list_enabled`` 才是运行期装载的取数口：
    是否实例化由 ``is_enabled`` 单独表达，读取口不替调用方把停用的行藏起来。
    """

    def __init__(
            self,
            *,
            get: InstanceReader = _empty_instance_get,
            list_all: InstanceLister = _empty_instance_list,
            list_by_source: InstanceSourceLister = _empty_instance_list_by_source,
            save: InstanceWriter = _ignore_instance_save,
            delete: InstanceDeleter = _ignore_instance_delete,
            list_enabled: InstanceLister = _empty_instance_list,
            set_enabled: InstanceEnabler = _ignore_instance_enable,
    ) -> None:
        """保存由启动组合根提供的实例表读写函数。"""
        self._get = get
        self._list_all = list_all
        self._list_by_source = list_by_source
        self._save = save
        self._delete = delete
        self._list_enabled = list_enabled
        self._set_enabled = set_enabled

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
        """按实例 ID 彻底删除一行，连同其配置，返回删除前是否存在。"""
        return self._delete(instance_id)

    def list_enabled(self) -> list[PluginInstance]:
        """列出应当被实例化并启动的配置，不含停用的。"""
        return self._list_enabled()

    def set_enabled(self, instance_id: str, is_enabled: bool) -> bool:
        """写入启用位，返回该行是否存在。"""
        return self._set_enabled(instance_id, is_enabled)


_plugin_instance_directory = PluginInstanceDirectory()


def configure_plugin_instance_directory(directory: PluginInstanceDirectory) -> None:
    """由启动组合根替换插件实例描述符表持久化实现。"""
    global _plugin_instance_directory
    _plugin_instance_directory = directory


def get_plugin_instance_directory() -> PluginInstanceDirectory:
    """返回当前插件实例描述符表持久化端口。"""
    return _plugin_instance_directory


class PluginInstanceStore:
    """管理共享源码的分身实例与源插件本体自身的记录，二者互不进入对方视图。

    两类记录同存一张表，靠 ``instance_id`` 是否等于 ``source_plugin_id`` 区分：
    ``all()``/``enabled()``/``get()``/``save()``/``disable()``/``purge()`` 只服务
    分身，``get_host()``/``save_host()``/``disable_host()`` 只服务本体。任何一侧都
    读不到、也改不到对方的记录。

    读取口返回全部登记行（含停用的）；是否应当被实例化由 ``is_enabled`` 单独表达，
    运行期取数走 ``enabled()``。
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
        """旧 systemconfig 单键的内容向独立表兜底导入一次，且只导入一次。

        导入完成后落一个持久化标记。判据不能是「表当前为空」：旧键刻意保留作
        回滚依据、从不清理，用户把分身全部删光后表就会重新变空，下次进程启动
        会把已删除的分身整批导回来，删一次复活一次。进程内另外维护一个已检查
        标志，避免每次访问都为此多打一次查询。

        :raise Exception: 导入失败时向上抛出，不吞掉持久化层错误
        """
        if self._bootstrap_checked:
            return
        self._bootstrap_checked = True
        storage = self._storage()
        if storage.read(SystemConfigKey.PluginInstancesImported):
            return
        directory = self._directory()
        if not directory.list_all():
            for instance in self._legacy_instances().values():
                directory.save(instance)
        storage.write(SystemConfigKey.PluginInstancesImported, True)

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
        """读取全部登记的分身实例（含停用的），不含源插件本体自身的记录。

        停用的分身照常返回：它仍是一份登记在册的配置，卡片与版本绑定视图都要看得见。
        运行期该装载哪些用 :meth:`enabled`。
        """
        self._ensure_bootstrapped()
        return {
            record.instance_id: record
            for record in self._directory().list_all()
            if not record.is_host
        }

    def enabled(self) -> dict[str, PluginInstance]:
        """读取应当被实例化并启动的分身实例，不含停用的与本体记录。"""
        self._ensure_bootstrapped()
        return {
            record.instance_id: record
            for record in self._directory().list_enabled()
            if not record.is_host
        }

    def get(self, instance_id: str) -> PluginInstance | None:
        """读取指定在册分身实例；本体自身与已卸载的分身都不会从这里返回。"""
        record = self.get_any(instance_id)
        return record if record is not None and record.is_enabled else None

    def get_any(self, instance_id: str) -> PluginInstance | None:
        """读取指定分身实例，含已卸载的；本体自身的记录不会从这里返回。

        供恢复路径使用：同后缀重建撞上的正是一个已卸载的分身，要看得见它才能把
        配置与展示信息一并拿回来。

        精确匹配未命中时按大小写不敏感再找一次，与 :meth:`get_host` 同一口径。
        分身 ID 由源插件 ID 直接拼后缀而成，而调用方手里的那份大小写未必与落盘的
        一致；只做精确匹配会让判存通过（运行态查询本就大小写不敏感）而写入落空，
        表现为卸载回报成功、启用位纹丝不动。
        """
        self._ensure_bootstrapped()
        record = self._directory().get(instance_id)
        if record is not None:
            return record if not record.is_host else None
        target = instance_id.casefold()
        for candidate in self._directory().list_all():
            if candidate.is_host or candidate.instance_id.casefold() != target:
                continue
            return candidate
        return None

    def all_hosts(self) -> dict[str, PluginInstance]:
        """一次性读取全部源插件本体记录，含已停用的，不含分身实例。

        供目录投影批量取数使用，按插件 ID 遍历卡片时只做内存字典查找，不再逐张卡片
        各查一次数据库。这里不按启用位过滤：本体是否出现在清单里由安装清单决定，
        这些行只提供展示覆盖，漏掉停用的那份反而会让卡片丢掉自定义名称与图标。
        """
        self._ensure_bootstrapped()
        return {
            record.instance_id: record
            for record in self._directory().list_all()
            if record.is_host
        }

    def enabled_hosts(self) -> dict[str, PluginInstance]:
        """读取应当被装载的源插件本体记录，不含停用的与分身实例。

        本体与分身在这里终于用同一个判据：``is_enabled`` 决定一份配置是否应当被
        实例化并启动。安装清单退回去只回答「这个插件的包在不在磁盘上」，不再兼任
        启用开关——两者本是两件事，一处安装记录同时充当运行开关会让「装着但先不跑」
        无从表达。
        """
        self._ensure_bootstrapped()
        return {
            record.instance_id: record
            for record in self._directory().list_enabled()
            if record.is_host
        }

    def save(self, instance: PluginInstance) -> None:
        """新增或更新分身实例，并以实例 ID 作为稳定持久化键。

        分身的实例 ID 必须区别于其源插件 ID：两者相等的那一行表示的是本体自身，
        按分身写入会把本体的记录顶掉。
        """
        self._ensure_bootstrapped()
        if instance.is_host:
            raise ValueError(
                f"分身实例 {instance.instance_id} 的 ID 不能等于其源插件 ID"
            )
        self._directory().save(instance)

    def disable(self, instance_id: str) -> bool:
        """卸载指定分身实例，返回卸载前它是否存在且处于启用状态。

        卸载就是把启用位置假：配置、日志等级覆盖与展示信息原样留在那一行，再次启用
        即恢复。要连配置一并抹掉走 :meth:`purge`。
        """
        self._ensure_bootstrapped()
        record = self.get(instance_id)
        if record is None or not record.is_enabled:
            return False
        # 用解析出的规范 ID 写回：调用方给的那份大小写未必与落盘的一致
        return self._directory().set_enabled(record.instance_id, False)

    def enable(self, instance_id: str) -> bool:
        """启用指定分身实例，返回启用前它是否存在且处于停用状态。"""
        self._ensure_bootstrapped()
        record = self.get_any(instance_id)
        if record is None or record.is_enabled:
            return False
        return self._directory().set_enabled(record.instance_id, True)

    def purge(self, instance_id: str) -> bool:
        """彻底删除指定分身实例连同其配置，返回删除前该行是否存在。

        这是用户主动放弃恢复时的唯一出口；卸载本身不删行。
        """
        self._ensure_bootstrapped()
        record = self.get_any(instance_id)
        return self._directory().delete(record.instance_id if record else instance_id)

    def for_source(self, source_plugin_id: str) -> list[PluginInstance]:
        """按持久化顺序返回引用同一源插件的在册分身，不含本体与已卸载的。

        卸载即停用，被卸载的分身不再是一个在册实例：它不该出现在实例列表、不该被
        重载、不该参与默认调用目标的候选。想拿回它要走同后缀重建那条恢复路径。
        """
        self._ensure_bootstrapped()
        return [record for record in self.all_for_source(source_plugin_id) if record.is_enabled]

    def all_for_source(self, source_plugin_id: str) -> list[PluginInstance]:
        """按持久化顺序返回引用同一源插件的全部分身，含已卸载的，不含本体记录。

        版本回收要用它：已卸载的分身仍锚定着某个版本，那个版本目录不能被当成无人
        引用而删掉，否则恢复出来的分身会落到一个不存在的版本上。
        """
        self._ensure_bootstrapped()
        return [
            record
            for record in self._directory().list_by_source(source_plugin_id)
            if not record.is_host
        ]

    def disabled_for_source(self, source_plugin_id: str) -> list[PluginInstance]:
        """返回引用同一源插件、已卸载且留有设置的分身。

        这是恢复选择器的取数口：卸载只把启用位置假、配置与展示信息原样留着，用户
        在创建分身时可以挑一个旧实例把它连同配置一起拿回来。
        """
        return [
            record
            for record in self.all_for_source(source_plugin_id)
            if not record.is_enabled
        ]

    def disable_host(self, plugin_id: str) -> bool:
        """停用源插件本体但保留其全部设置，返回停用前它是否存在且处于启用状态。

        纯停用：锚定版本原样留着，因为插件包还在磁盘上，再次启用应当回到同一个
        版本。这与卸载不同——卸载要清锚定版本，见 :meth:`retire_host`。
        """
        self._ensure_bootstrapped()
        host = self.get_host(plugin_id)
        if host is None or not host.is_enabled:
            return False
        return self._directory().set_enabled(host.instance_id, False)

    def retire_host(self, plugin_id: str) -> bool:
        """插件卸载收尾：停用本体并清掉锚定版本，返回收尾前它是否存在。

        锚定版本必须清除：它指向的版本目录会随插件包一起消失，留着会让重装同名
        插件按早已删除的版本目录解析源码。默认目标置位与日志等级覆盖由启用位置假
        时一并清掉。业务参数则保留——那是用户的数据，重装后应当还在。分身记录不会
        从这里被处理。
        """
        self._ensure_bootstrapped()
        host = self.get_host(plugin_id)
        if host is None:
            return False
        if host.pinned_version is not None:
            self._directory().save(host.model_copy(update={"pinned_version": None}))
        return self._directory().set_enabled(host.instance_id, False)

    def enable_host(self, plugin_id: str, *, pinned_version: str | None = None) -> None:
        """把源插件本体登记为应当装载，没有本体记录时按默认视图建出。

        安装完成后必须调用：本体的装载判据已经归口到 ``is_enabled``，只往安装清单
        里加一条而不建出这一行，插件会装完却不加载。
        """
        self._ensure_bootstrapped()
        host = self.get_host(plugin_id)
        if host is None:
            self.save_host(
                PluginInstance(
                    instance_id=plugin_id,
                    source_plugin_id=plugin_id,
                    pinned_version=pinned_version,
                    is_enabled=True,
                )
            )
            return
        self._directory().set_enabled(host.instance_id, True)

    def get_host(self, plugin_id: str) -> PluginInstance | None:
        """读取源插件本体的版本绑定记录；从未显式绑定过版本时为 None。

        精确匹配未命中时按大小写不敏感再找一次：调用方既有拿着插件规范 ID 来的
        （版本绑定、日志等级、默认目标），也有拿着磁盘目录名来的（加载器解析本体
        源码目录，目录名恒为小写）。只做精确匹配会让后者一律落空，本体钉定的版本
        永远读不到，表现为钉了版本、接口回报成功，实际仍加载当前版本。插件 ID 在
        大小写不敏感的意义下唯一（同名不同大小写的版本目录在安装期即被拒绝），
        因此回落匹配不会产生歧义；常见调用都会命中精确匹配，不额外查库。
        """
        self._ensure_bootstrapped()
        record = self._directory().get(plugin_id)
        if record is not None:
            return record if record.is_host else None
        target = plugin_id.casefold()
        for instance_id, host_record in self.all_hosts().items():
            if instance_id.casefold() == target:
                return host_record
        return None

    def save_host(self, instance: PluginInstance) -> None:
        """新增或更新源插件本体的记录，本体的 ``instance_id`` 恒等于其自身 ID。"""
        self._ensure_bootstrapped()
        self._directory().save(
            instance.model_copy(update={"source_plugin_id": instance.instance_id})
        )


_plugin_storage = PluginStorage()


def configure_plugin_storage(storage: PluginStorage) -> None:
    """由启动组合根替换插件运行时持久化实现。"""
    global _plugin_storage
    _plugin_storage = storage


def get_plugin_storage() -> PluginStorage:
    """返回当前插件运行时持久化端口。"""
    return _plugin_storage
