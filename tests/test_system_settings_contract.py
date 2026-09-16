"""Agent 系统设置完整合同、Schema、写入边界和并发语义测试。"""

from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.application.settings as settings_module
from app.agent.policy.api import API_OPERATION_ROUTES
from app.agent.tools.impl.api import MoviePilotApiTool
from app.application.settings.contract import (
    ALL_SETTING_SPECS,
    CORE_SETTING_SPECS,
    SETTING_GROUPS,
    SYSTEMCONFIG_SETTING_SPECS,
    build_value_schema,
)
from app.runtime.config import Settings
from app.schemas.types import SystemConfigKey


def test_settings_contract_covers_every_declared_source() -> None:
    """合同必须覆盖全部 Settings 字段和全部 SystemConfigKey，且每项有来源说明。"""
    assert set(CORE_SETTING_SPECS) == set(Settings.model_fields)
    assert set(SYSTEMCONFIG_SETTING_SPECS) == {item.value for item in SystemConfigKey}
    assert len(ALL_SETTING_SPECS) == len(Settings.model_fields) + len(SystemConfigKey)
    assert SETTING_GROUPS

    for spec in ALL_SETTING_SPECS.values():
        assert spec.description
        assert spec.group
        assert spec.source_file
        assert spec.source_line > 0
        schema = build_value_schema(spec)
        assert schema.get("description") == spec.description
        assert isinstance(schema, dict)


def test_settings_contract_exposes_machine_readable_boundaries() -> None:
    """合同应公开单位、示例、依赖和复杂设置的专用 operation 边界。"""
    runtime_spec = ALL_SETTING_SPECS["ACCESS_TOKEN_EXPIRE_MINUTES"]
    assert runtime_spec.unit == "minutes"
    assert runtime_spec.examples
    runtime_schema = build_value_schema(runtime_spec)
    assert runtime_schema["x-unit"] == "minutes"
    assert runtime_schema["examples"] == list(runtime_spec.examples)
    assert build_value_schema(ALL_SETTING_SPECS["API_WORKERS"])["minimum"] == 1

    managed = ALL_SETTING_SPECS[SystemConfigKey.CustomFilterRules.value]
    assert managed.generic_write_allowed is False
    assert managed.update_operations == ()
    assert managed.preferred_operation_ids
    assert managed.apply_mode == "dedicated_operation"

    dependent = ALL_SETTING_SPECS[SystemConfigKey.SearchFilterRuleGroups.value]
    assert SystemConfigKey.UserFilterRuleGroups.value in dependent.dependencies


def test_settings_catalog_is_lightweight_and_value_free() -> None:
    """list 阶段只返回合同索引，不应读取当前配置值。"""
    runtime = MagicMock()
    system = MagicMock()
    service = settings_module.SystemSettingsService(runtime, system, AsyncMock())

    result = service.catalog(group="ai_agent", limit=2)

    assert result["returned_count"] <= 2
    assert result["settings"]
    assert all("value" not in item for item in result["settings"])
    assert all("value_schema" not in item for item in result["settings"])
    assert all("update_operations" in item for item in result["settings"])
    runtime.get.assert_not_called()
    system.get.assert_not_called()


def test_settings_describe_returns_schema_value_and_revision() -> None:
    """describe 阶段应返回单项完整 schema、当前值和稳定 revision。"""
    runtime = MagicMock()
    runtime.get.return_value = "gpt-test"
    system = MagicMock()
    service = settings_module.SystemSettingsService(runtime, system, AsyncMock())

    result = service.describe(setting_key="LLM_MODEL")

    assert result["setting_key"] == "LLM_MODEL"
    assert result["value"] == "gpt-test"
    assert result["revision"].startswith("sha256:")
    assert result["definition"]["value_schema"]["type"] == "string"
    assert result["definition"]["persistence"] == "app.env"


def test_structured_sensitive_settings_preserve_non_secret_match_fields() -> None:
    """复杂敏感配置只隐藏凭据字段，保留名称和类型供 Agent 定位条目。"""
    spec = ALL_SETTING_SPECS[SystemConfigKey.Notifications.value]
    redacted = settings_module.redact_setting_value(
        spec,
        [{"name": "telegram-main", "type": "telegram", "config": {"token": "secret"}}],
    )
    assert redacted[0]["name"] == "telegram-main"
    assert redacted[0]["type"] == "telegram"
    assert redacted[0]["config"]["token"] == "***"


@pytest.mark.asyncio
async def test_settings_update_rejects_managed_and_internal_generic_writes(monkeypatch) -> None:
    """通用写入口必须拒绝专用 operation 和内部状态设置。"""
    runtime = MagicMock()
    system = MagicMock()
    publish = AsyncMock()
    monkeypatch.setattr(settings_module, "plugin_system_config_mutation", lambda _key: nullcontext())
    service = settings_module.SystemSettingsService(runtime, system, publish)

    with pytest.raises(ValueError, match="禁止通用写入"):
        await service.update(
            setting_key=SystemConfigKey.CustomFilterRules.value,
            value=[],
        )
    with pytest.raises(ValueError, match="内部状态"):
        await service.update(
            setting_key=SystemConfigKey.AIAgentConfig.value,
            value={},
        )
    runtime.update.assert_not_called()
    system.async_set_with_normalized_value.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_settings_revision_is_optional_but_protects_conditional_writes(monkeypatch) -> None:
    """旧调用可省略 revision，新调用提供旧 revision 时必须拒绝并发覆盖。"""
    runtime = MagicMock()
    state = {"value": "old"}
    runtime.get.side_effect = lambda _key: state["value"]

    def update(_key, value):
        """更新测试中的运行时设置快照。"""
        state["value"] = value
        return True, "updated"

    runtime.update.side_effect = update
    system = MagicMock()
    publish = AsyncMock()
    monkeypatch.setattr(settings_module, "plugin_system_config_mutation", lambda _key: nullcontext())
    service = settings_module.SystemSettingsService(runtime, system, publish)

    legacy = await service.update(setting_key="LLM_MODEL", value="legacy")
    assert legacy["changed"] is True
    revision = legacy["revision"]

    success = await service.update(
        setting_key="LLM_MODEL",
        value="next",
        expected_revision=revision,
    )
    assert success["saved_value"] == "next"

    with pytest.raises(settings_module.SystemSettingConflictError, match="expected_revision"):
        await service.update(
            setting_key="LLM_MODEL",
            value="stale",
            expected_revision=revision,
        )
    assert state["value"] == "next"


def test_setting_key_resolution_accepts_enum_names_and_values() -> None:
    """设置解析应同时支持 SystemConfigKey 的枚举名和值。"""
    by_name = settings_module.resolve_setting_spec(SystemConfigKey.Downloaders.name)
    by_value = settings_module.resolve_setting_spec(SystemConfigKey.Downloaders.value)
    assert by_name is not None
    assert by_name == by_value
    assert by_name.systemconfig_key is SystemConfigKey.Downloaders


def test_system_setting_operations_expose_the_three_stage_contract() -> None:
    """Agent 网关必须公开 list、describe、get 和 update 的固定路由与参数分支。"""
    assert API_OPERATION_ROUTES["config.system.list"].method == "GET"
    assert API_OPERATION_ROUTES["config.system.list"].path == "/api/v1/system/settings/catalog"
    assert API_OPERATION_ROUTES["config.system.describe"].method == "GET"
    assert API_OPERATION_ROUTES["config.system.describe"].path == "/api/v1/system/settings/describe/{setting_key}"

    schema = MoviePilotApiTool(session_id="contract", user_id="1").get_mcp_input_schema()
    branches = {
        item["properties"]["operation_id"]["const"]: item
        for item in schema["oneOf"]
    }
    assert branches["config.system.list"]["properties"]["query"]["properties"]["limit"]["maximum"] == 500
    assert branches["config.system.describe"]["properties"]["path_params"]["required"] == ["setting_key"]
    update_body_ref = branches["config.system.update"]["properties"]["body"]["$ref"]
    update_body = schema["$defs"][update_body_ref.rsplit("/", 1)[-1]]
    assert "expected_revision" in update_body["properties"]
