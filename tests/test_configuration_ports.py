"""配置快照与窄读写端口测试。"""

import asyncio
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest

from app.application.configuration import (
    ApiRuntimeConfig,
    ChainRuntimeConfig,
    RuntimeConfiguration,
    RuntimeSettingsService,
    SchedulerRuntimeConfig,
    SystemConfigService,
    TransferRetryConfig,
    configure_runtime_configuration,
    configure_transfer_retry_config,
    get_api_runtime_config_snapshot,
    get_transfer_retry_config,
)
from app.application.security.userconfig import UserConfigurationService
from app.schemas.types import SystemConfigKey


class _InlineDatabaseExecutor:
    """同步执行测试操作，并保留异步应用端口的调用形态。"""

    async def run(self, operation):
        """执行并返回操作结果。"""
        return operation()


class _MutableSettings:
    """记录管理设置服务的读取和更新操作。"""

    def __init__(self) -> None:
        """初始化一组可变测试设置。"""
        self.VALUE = "before"

    def model_dump(self, *, include=None, exclude=None):
        """按白名单返回设置字典。"""
        values = {"VALUE": self.VALUE, "SECRET": "hidden"}
        if include is not None:
            values = {key: value for key, value in values.items() if key in include}
        if exclude is not None:
            values = {key: value for key, value in values.items() if key not in exclude}
        return values

    def update_settings(self, env):
        """批量更新并返回逐项结果。"""
        for key, value in env.items():
            setattr(self, key, value)
        return {key: (True, "") for key in env}

    def update_setting(self, key, value):
        """更新单个设置。"""
        setattr(self, key, value)
        return True, ""


def test_runtime_settings_service_hides_mutable_settings_implementation() -> None:
    """管理 API 通过窄服务读取和修改设置，不依赖全局 Settings 类型。"""
    settings = _MutableSettings()
    service = RuntimeSettingsService(settings)

    assert service.contains("VALUE")
    assert service.get("VALUE") == "before"
    assert service.snapshot(include={"VALUE"}) == {"VALUE": "before"}
    assert service.update("VALUE", "after") == (True, "")
    assert service.update_many({"VALUE": "final"}) == {"VALUE": (True, "")}
    assert service.get("VALUE") == "final"


def test_runtime_settings_service_is_the_only_mutable_settings_port() -> None:
    """可变部署设置只通过应用服务暴露，不再提供宿主级兼容代理。"""
    service = RuntimeSettingsService(_MutableSettings())

    assert service.snapshot(include={"VALUE"}) == {"VALUE": "before"}


def test_system_config_service_supports_separate_reader_and_writer() -> None:
    """应用服务可以分别注入只读与写入适配器。"""
    reader = MagicMock()
    reader.get.return_value = "old"
    writer = MagicMock()
    writer.set.return_value = True
    writer.delete.return_value = True
    service = SystemConfigService(
        reader=reader,
        writer=writer,
        async_executor=_InlineDatabaseExecutor(),
    )

    assert service.get("key") == "old"
    assert service.set("key", "new") is True
    assert asyncio.run(service.async_set("key", "new")) is True
    service.delete("key")
    assert asyncio.run(service.async_delete("key")) is True

    reader.get.assert_called_once_with("key")
    assert writer.set.call_args_list == [
        (("key", "new"), {}),
        (("key", "new"), {}),
    ]
    assert writer.delete.call_args_list == [
        (("key",), {}),
        (("key",), {}),
    ]


def test_system_config_service_forwards_atomic_increment() -> None:
    """共享识别命中计数应通过应用服务转发到持久化原子递增端口。"""
    reader = MagicMock()
    writer = MagicMock()
    writer.increment.return_value = 3
    service = SystemConfigService(reader=reader, writer=writer)

    assert service.increment(SystemConfigKey.MediaRecognizeShareCount) == 3

    writer.increment.assert_called_once_with(SystemConfigKey.MediaRecognizeShareCount, 1)


def test_system_config_service_normalizes_sync_and_async_writes() -> None:
    """同步和异步配置写入必须共用组合根注入的值规范化边界。"""
    reader = MagicMock()
    writer = MagicMock()
    writer.set.return_value = True
    normalizer = MagicMock(side_effect=lambda key, value: {"key": str(key), "value": value})
    service = SystemConfigService(
        reader=reader,
        writer=writer,
        async_executor=_InlineDatabaseExecutor(),
        value_normalizer=normalizer,
    )

    assert service.set("demo", 1) is True
    assert asyncio.run(service.async_set("demo", 2)) is True
    assert writer.set.call_args_list == [
        (("demo", {"key": "demo", "value": 1}), {}),
        (("demo", {"key": "demo", "value": 2}), {}),
    ]
    assert normalizer.call_count == 2


def test_user_configuration_service_supports_sync_and_async_writes() -> None:
    """用户配置服务的同步与异步入口执行同一仓储方法。"""
    repository = MagicMock()
    repository.set.return_value = True
    service = UserConfigurationService(
        repository,
        async_executor=_InlineDatabaseExecutor(),
    )

    assert service.set("alice", "theme", "dark") is None
    assert asyncio.run(service.async_set("alice", "theme", "light")) is None
    assert repository.set.call_args_list == [
        ((), {"username": "alice", "key": "theme", "value": "dark"}),
        ((), {"username": "alice", "key": "theme", "value": "light"}),
    ]


def test_transfer_retry_provider_returns_frozen_snapshot_per_call() -> None:
    """配置工厂在每次用例入口创建新快照，旧快照不受 reload 后状态影响。"""
    state = {"value": 2}
    configure_transfer_retry_config(
        lambda: TransferRetryConfig(max_failed_retries=state["value"])
    )

    before_reload = get_transfer_retry_config()
    state["value"] = 4
    after_reload = get_transfer_retry_config()

    assert before_reload.max_failed_retries == 2
    assert after_reload.max_failed_retries == 4


def test_api_runtime_provider_returns_frozen_snapshot_per_request() -> None:
    """API 每次请求读取新配置，但已取得的快照保持不变。"""
    state = {"enabled": False}
    configure_runtime_configuration(
        RuntimeConfiguration(
            api=lambda: ApiRuntimeConfig(
                access_token_expire_minutes=60,
                btrfs_fsid_dedup=False,
                ai_agent_enable=state["enabled"],
            ),
            scheduler=lambda: SchedulerRuntimeConfig(
                False, "Asia/Shanghai", 1, False, "", None, None, False,
                24, "rss", 30, False, None, None, False, None, False, None,
            ),
            chain=lambda: ChainRuntimeConfig(media_extensions=(".mkv",)),
        )
    )

    before_reload = get_api_runtime_config_snapshot()
    state["enabled"] = True
    after_reload = get_api_runtime_config_snapshot()

    assert before_reload.ai_agent_enable is False
    assert after_reload.ai_agent_enable is True


def test_chain_runtime_config_is_an_instance_scoped_frozen_snapshot() -> None:
    """Chain 配置应随实例固定，避免同一业务调用中途读取到 reload 后的新值。"""
    snapshot = ChainRuntimeConfig(
        media_extensions=(".mkv",),
        superuser="root",
        media_recognize_share=True,
        resource_url="https://example.test/#/resource",
    )

    assert snapshot.superuser == "root"
    assert snapshot.media_recognize_share is True
    assert snapshot.resource_url == "https://example.test/#/resource"
    with pytest.raises(FrozenInstanceError):
        snapshot.superuser = "changed"  # type: ignore[misc]
