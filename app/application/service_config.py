"""服务实例配置应用服务与组合根注入点。

下载器、媒体服务器、消息渠道、存储与登录认证的实例配置由服务实例配置表承载。表在持久
化层、消费方在运行期与接口层，本服务是两者之间那一层：对外收发两种形状——「整族配置
列表」与这几族配置在设置页上的形状一致，「单条配置行」与表里那一行逐列对应——对内负责
摊平成行、按消费方分列、整族覆盖与逐条增删改。

读取带一层进程内缓存。取服务是热路径，每次取用都会重读整族配置；系统设置本来就常驻
内存（``SystemConfigOper`` 在构造时一次性载入），切到表后若每次取用都查一次库，换来的
是一条比原先慢的热路径。缓存只在本服务的写入口失效，绕过本服务直接改库不在其内。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Protocol

from app.application.configuration import get_configured_system_config
from app.runtime.extensions.service_config import (
    service_bare_token_field,
    service_capability,
)
from app.runtime.extensions.admission.service_config import (
    elect_bare_token_holder,
    service_config_records,
)


class ServiceConfigRepository(Protocol):
    """服务实例配置所需的最小持久化端口。"""

    def list_payloads(self, capability: str) -> List[dict]:
        """列出某族全部实例配置的摊平形状。"""

    def replace_capability(self, capability: str, records: List[dict]) -> int:
        """用给定的整族配置覆盖某族现有配置。"""

    def list_rows(self, capability: str) -> List[dict]:
        """列出某族全部实例配置的行形状。"""

    def list_rows_by_type(self, capability: str, service_type: str) -> List[dict]:
        """列出某族某类型全部实例配置的行形状。"""

    def get_row(self, capability: str, service_type: str, name: str) -> Optional[dict]:
        """按身份三元组取单条实例配置的行形状。"""

    def add_row(self, capability: str, record: dict) -> dict:
        """按配置行新增一条实例配置。"""

    def update(self, capability: str, service_type: str, name: str, payload: dict) -> bool:
        """更新单条实例配置的可写列。"""

    def delete(self, capability: str, service_type: str, name: str) -> bool:
        """删除单条实例配置。"""

    def set_default_target(self, capability: str, service_type: str, name: str) -> bool:
        """把某族的默认调用目标改为指定实例。"""

    def clear_default_target(self, capability: str) -> int:
        """清除某族的默认调用目标置位。"""


class ServiceInstanceConfigService:
    """服务实例配置的整族读写与逐条读写应用服务。

    两种写入口并存而不是互相取代：整族覆盖对应设置页一次性交出整份列表，逐条写入
    对应配置列表页上单独增删改一条。逐条写入不读整族、也不回写整族，因此两位管理员
    同时改不同的配置不会互相覆盖；整族覆盖仍是「整份替换」，谁后提交谁的整份生效。

    两条入口共用同一份读取缓存失效：缓存服务的是取服务这条热路径，绕过本服务直接改库
    才不在其内，而逐条写入正是经由本服务，因此不会出现「表已改、实例还照旧」。
    """

    def __init__(self, repository: ServiceConfigRepository) -> None:
        """注入服务实例配置数据端口。"""
        self._repository = repository
        self._lock = threading.RLock()
        self._cache: Dict[str, List[dict]] = {}

    def read(self, capability: Optional[str]) -> List[dict]:
        """
        读取某族的全部实例配置。

        :param capability: 族标识
        :return: 该族全部配置字典；族标识为空时为空列表
        """
        if not capability:
            return []
        with self._lock:
            cached = self._cache.get(capability)
            if cached is not None:
                return cached
        payloads = self._repository.list_payloads(capability)
        with self._lock:
            self._cache[capability] = payloads
        return payloads

    def save(self, capability: Optional[str], value: Any) -> bool:
        """
        用给定的整族配置覆盖某族现有配置。

        入参是设置页与智能体交出的整族配置列表，与切表前写进 systemconfig 的形状一致；
        整形（分列、去重、默认调用目标裁决）在写入前完成。

        返回值按「写完之后这一族有没有变」判定，而不是按「有没有执行写入」：配置变更
        事件会触发整族模块重载，一次内容相同的保存不该把服务重启一遍。

        :param capability: 族标识
        :param value: 整族配置列表，为 None 时视为清空该族
        :return: 该族内容是否发生变化；族标识为空时为 False
        """
        return self.save_records(capability, service_config_records(capability, value or []))

    def save_records(self, capability: Optional[str], records: List[dict]) -> bool:
        """
        用已整形好的整族配置行覆盖某族现有配置。

        入参是表的行形状，供不按三族配置模型整形的族直接写入；返回值判定与 `save`
        相同，按「写完之后这一族有没有变」而不是「有没有执行写入」。

        :param capability: 族标识
        :param records: 该族的全部配置行
        :return: 该族内容是否发生变化；族标识为空时为 False
        """
        if not capability:
            return False
        before = self.read(capability)
        self._repository.replace_capability(capability, records)
        self.invalidate(capability)
        return self.read(capability) != before

    def list_rows(self, capability: Optional[str]) -> List[dict]:
        """
        列出某族全部实例配置的行形状。

        不走读取缓存：缓存服务的是取服务那条热路径，读的是摊平形状；配置列表页每次
        打开都该看到库里当下的内容，拿一份为别的读者缓存的副本只会让刚改完的配置看
        起来没生效。

        :param capability: 族标识
        :return: 该族全部配置行；族标识为空时为空列表
        """
        if not capability:
            return []
        return self._repository.list_rows(capability)

    def get_row(
        self, capability: Optional[str], service_type: str, name: str
    ) -> Optional[dict]:
        """
        按身份三元组取单条实例配置的行形状。

        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :return: 命中的配置行；不存在或族标识为空时为 None
        """
        if not capability:
            return None
        return self._repository.get_row(capability, service_type, name)

    def create_record(self, capability: str, record: dict) -> dict:
        """
        新增一条实例配置。

        新增的实例一律不是默认调用目标，该置位由专用入口显式设定。

        :param capability: 族标识
        :param record: 配置行，含 type/name/enabled/config/host_config/provider
        :return: 新增后的配置行
        :raises ServiceConfigNameConflictError: 同族同类型下已有同名配置
        """
        created = self._repository.add_row(capability, record)
        self._settle_bare_token_pointers(capability, created["type"])
        self.invalidate(capability)
        return self._repository.get_row(capability, created["type"], created["name"]) or created

    def update_record(
        self, capability: str, service_type: str, name: str, payload: dict
    ) -> bool:
        """
        更新单条实例配置的可写列。

        只发一条按身份三元组定位的更新语句，不读整族也不回写整族，因此同族其余配置
        不受影响。

        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :param payload: 待写入的列值
        :return: 是否更新了配置行
        :raises ServiceConfigNameConflictError: 改名后与同族同类型下的既有配置重名
        """
        updated = self._repository.update(capability, service_type, name, payload)
        if updated:
            self._settle_bare_token_pointers(capability, service_type)
            self.invalidate(capability)
        return updated

    def delete_record(self, capability: str, service_type: str, name: str) -> bool:
        """
        删除单条实例配置。

        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :return: 是否删除了配置行
        """
        deleted = self._repository.delete(capability, service_type, name)
        if deleted:
            self._settle_bare_token_pointers(capability, service_type)
            self.invalidate(capability)
        return deleted

    def set_default_target(self, capability: str, service_type: str, name: str) -> bool:
        """
        把某族的默认调用目标改为指定实例。

        目标实例没有配置行时不动原有置位：先清后置一旦在目标缺席时执行到一半，该族会
        从「有默认调用目标」变成「没有」。

        :param capability: 族标识
        :param service_type: 类型标识
        :param name: 实例名
        :return: 是否完成置位
        """
        placed = self._repository.set_default_target(capability, service_type, name)
        if placed:
            self.invalidate(capability)
        return placed

    def clear_default_target(self, capability: str) -> int:
        """
        清除某族的默认调用目标置位。

        :param capability: 族标识
        :return: 清除的行数
        """
        cleared = self._repository.clear_default_target(capability)
        if cleared:
            self.invalidate(capability)
        return cleared

    def _settle_bare_token_pointers(self, capability: str, service_type: str) -> None:
        """
        为该类型重新裁出恰好一个裸令牌兼容指针。

        只有声明了兼容指针的族（当前仅存储）需要这一步。逐条写入动的是单行，而「每个
        类型恰好一条指针」是类型范围内的不变量：删掉承接指针的那一份之后若不重裁，该
        类型的存量裸路径会整体解析不到实例。裁决规则与整族写入共用一份实现，两条写入
        口因而不会给出不同的指向；只有标记确实需要翻转的行才发更新语句。

        :param capability: 族标识
        :param service_type: 类型标识
        :return: 无返回值
        """
        field = service_bare_token_field(capability)
        if not field:
            return
        rows = self._repository.list_rows_by_type(capability, service_type)
        chosen = elect_bare_token_holder(field, rows)
        for row in rows:
            expected = row is chosen
            host_config = dict(row.get("host_config") or {})
            if bool(host_config.get(field)) is expected:
                continue
            host_config[field] = expected
            self._repository.update(
                capability, service_type, row["name"], {"host_config": host_config}
            )

    def invalidate(self, capability: Optional[str] = None) -> None:
        """
        丢弃读取缓存。

        :param capability: 族标识，为空时丢弃全部族的缓存
        :return: 无返回值
        """
        with self._lock:
            if capability:
                self._cache.pop(capability, None)
            else:
                self._cache.clear()


_configured_service_instance_configs: ServiceInstanceConfigService | None = None


def configure_service_instance_configs(service: ServiceInstanceConfigService) -> None:
    """由启动组合根登记服务实例配置服务。"""
    global _configured_service_instance_configs
    _configured_service_instance_configs = service


def get_configured_service_instance_configs() -> ServiceInstanceConfigService:
    """返回启动阶段登记的服务实例配置服务。"""
    if _configured_service_instance_configs is None:
        raise RuntimeError("服务实例配置服务尚未配置")
    return _configured_service_instance_configs


def read_system_setting(key: Any) -> Any:
    """
    按配置键读取系统设置值。

    服务实例配置的事实源已是服务实例配置表，systemconfig 上的同名键只停写不删，留作
    回退用的历史快照，读到的是切表当时的内容。凡是按配置键取值的入口都要走这里分流，
    否则一部分入口读表、另一部分读快照，用户会看到两份互相矛盾的配置。

    :param key: 配置键，接受 `SystemConfigKey` 成员或其取值字符串
    :return: 配置值
    """
    key_value = getattr(key, "value", key)
    capability = service_capability(key_value)
    if capability:
        return get_configured_service_instance_configs().read(capability)
    return get_configured_system_config().get(key)


async def async_write_system_setting(key: Any, value: Any) -> bool:
    """
    按配置键写入系统设置值。

    :param key: 配置键，接受 `SystemConfigKey` 成员或其取值字符串
    :param value: 待写入的配置值
    :return: 配置内容是否发生变化
    """
    key_value = getattr(key, "value", key)
    capability = service_capability(key_value)
    if capability:
        return get_configured_service_instance_configs().save(capability, value)
    return await get_configured_system_config().async_set(key, value) is True
