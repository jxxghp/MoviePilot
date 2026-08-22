"""配置快照与窄读写端口测试。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.application.configuration import (
    ApiRuntimeConfig,
    ChainRuntimeConfig,
    RuntimeConfiguration,
    SchedulerRuntimeConfig,
    SystemConfigService,
    TransferRetryConfig,
    configure_runtime_configuration,
    configure_transfer_retry_config,
    get_api_runtime_config_snapshot,
    get_transfer_retry_config,
)


def test_system_config_service_supports_separate_reader_and_writer() -> None:
    """应用服务可以分别注入只读与写入适配器。"""
    reader = MagicMock()
    reader.get.return_value = "old"
    reader.async_get = AsyncMock(return_value="async-old")
    writer = MagicMock()
    writer.set.return_value = True
    writer.async_set = AsyncMock(return_value=True)
    service = SystemConfigService(reader=reader, writer=writer)

    assert service.get("key") == "old"
    assert service.set("key", "new") is True
    assert asyncio.run(service.async_get("key")) == "async-old"
    assert asyncio.run(service.async_set("key", "new")) is True
    service.delete("key")

    reader.get.assert_called_once_with("key")
    writer.set.assert_called_once_with("key", "new")
    writer.delete.assert_called_once_with("key")


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
                advanced_mode=False,
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
    configure_runtime_configuration,
    get_api_runtime_config_snapshot,
