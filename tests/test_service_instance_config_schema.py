"""服务实例类型的配置契约：契约自身校验、两处写入判定与构造判定。

判据见 docs/plugin-extension-architecture.md §4.4。契约与配置界面并列：界面是呈现，
契约是形状。声明了契约的类型，畸形配置在写入路径被退回并说明原因，在实例构造路径
被跳过且只影响它自己；未声明契约的类型行为与本字段加入之前完全一致。
"""

import asyncio
import json
from dataclasses import replace
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.tools.impl.update_system_settings import UpdateSystemSettingsTool
from app.api.endpoints.service import config_form as service_config_form_endpoint
from app.api.endpoints.system import set_setting as set_setting_endpoint
from app.runtime.deprecation import notices as notices_module
from app.runtime.deprecation import policy as deprecation_policy
from app.runtime.extensions import service_config as service_config_module
from app.runtime.extensions.registry import service_instance as registry_module
from app.runtime.extensions.contract.config_schema import (
    config_schema_violation,
    config_value_violations,
)
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.contract.declaration import ServiceInstanceDeclaration
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.admission.service_instance import (
    SERVICE_INSTANCE_SCHEMA_DEPRECATION,
    service_instance_declaration_violation,
)
from app.runtime.extensions.service_config import create_service_instance
from app.runtime.extensions.service_config import service_capability
from app.runtime.extensions.admission.service_config import service_config_write_violation
from app.runtime.extensions.registry.service_instance import (
    ServiceInstanceAdapter,
    ServiceInstanceEntry,
    service_instance_registry,
)
from app.schemas.service import ServiceConfigForm
from app.schemas.system import NotificationConf
from app.schemas.types import ModuleType, SystemConfigKey

# 一份合法契约：必填字符串、带范围的整数、枚举与嵌套对象，且不接受未声明字段
_VALID_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "title": "示例服务",
    "properties": {
        "host": {"type": "string", "title": "服务器地址", "minLength": 1},
        "port": {"type": "integer", "title": "端口", "minimum": 1, "maximum": 65535},
        "secure": {"type": "boolean", "default": False},
        "mode": {"type": "string", "enum": ["push", "pull"], "default": "push"},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "auth": {
            "type": "object",
            "properties": {"token": {"type": "string", "minLength": 4}},
            "required": ["token"],
            "additionalProperties": False,
        },
    },
    "required": ["host"],
    "additionalProperties": False,
}


class _DemoClient:
    """按关键字构造的服务客户端桩，带齐下载器与消息通知两族的必填方法。"""

    def __init__(self, name: Optional[str] = None, **kwargs: Any):
        """记录宿主填入的实例名与展开的配置内容。"""
        self.name = name
        self.config = kwargs

    def is_inactive(self) -> bool:
        """回答连接是否已断开，下载器族的重连回路直调它。"""
        return False

    def reconnect(self) -> bool:
        """重建连接，下载器族判定失活后直调它。"""
        return True

    def get_state(self) -> bool:
        """回答通道是否就绪，消息通知族的连通性测试直调它。"""
        return True


class _Plugin:
    """声明服务实例类型的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "契约插件"

    def __init__(self, declarations: List[Any]):
        """保存待交出的声明列表。"""
        self._declarations = declarations

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return True

    def get_render_mode(self):
        """返回插件渲染模式。"""
        return "vuetify", None

    def provides_service_instances(self):
        """返回声明的服务实例类型。"""
        return self._declarations


@pytest.fixture(autouse=True)
def _isolate_service_instance_registry() -> Iterator[None]:
    """快照并复原服务实例注册表，避免测试间相互污染。"""
    original = dict(service_instance_registry._adapters)
    try:
        yield
    finally:
        service_instance_registry._adapters.clear()
        service_instance_registry._adapters.update(original)


@pytest.fixture
def service_configs(monkeypatch) -> Iterator[Dict[str, list]]:
    """接管服务配置读取端口，用例改写字典即改写用户配置。"""
    values: Dict[str, list] = {}

    def reader(capability: str) -> Any:
        """按能力标签返回用例写入的原始配置列表。"""
        config_key = service_config_module.service_config_key(capability)
        return values.get(config_key.value) if config_key else None

    monkeypatch.setattr(
        service_config_module, "_service_instance_config_reader", reader
    )
    yield values


class _RecordingLogger:
    """记录告警与错误文本的日志端口替身。"""

    def __init__(self):
        self.warnings: List[str] = []
        self.errors: List[str] = []

    def warning(self, message: str) -> None:
        """记录一条告警。"""
        self.warnings.append(message)

    def error(self, message: str) -> None:
        """记录一条错误。"""
        self.errors.append(message)

    def info(self, message: str) -> None:
        """记录一条信息，用例不关心其内容。"""


@pytest.fixture
def extension_logger(monkeypatch) -> _RecordingLogger:
    """接管服务实例注册表的日志端口。"""
    log = _RecordingLogger()
    monkeypatch.setattr(registry_module, "logger", log)
    return log


def _notification_config(name: str, service_type: str = "demo_channel", **config: Any) -> dict:
    """构造一条通知配置的原始字典。

    :param name: 实例名
    :param service_type: 类型标识
    :param config: 该实例的配置内容
    :return: 与持久化形状一致的配置字典
    """
    return {"name": name, "type": service_type, "enabled": True, "config": config}


def _adapter(config_schema: Optional[Dict[str, Any]]) -> ServiceInstanceAdapter:
    """构造一个通知族的扩展服务实例适配器。

    :param config_schema: 该类型声明的配置契约
    :return: 适配器
    """
    return ServiceInstanceAdapter(
        ServiceInstanceEntry(
            capability=ModuleType.Notification.value,
            service_type="demo_channel",
            name="示例通道",
            distribution=ExtensionDistribution.MARKET,
            owner="DemoPlugin",
            impl=_DemoClient,
            config_schema=config_schema,
        )
    )


# ---------------------------------------------------------------- 契约自身校验


def test_declaration_with_valid_schema_is_accepted():
    """契约落在受支持子集内时整条声明照常通过契约校验。"""
    declaration = ServiceInstanceDeclaration(
        capability="downloader",
        type="schema_downloader",
        name="带契约的下载器",
        impl=_DemoClient,
        config_schema=_VALID_SCHEMA,
    )

    assert service_instance_declaration_violation(declaration) is None


@pytest.mark.parametrize(
    "config_schema",
    [
        "not-a-mapping",
        {"type": "array", "properties": {}},
        {"type": "object", "$defs": {}},
        {"type": "object", "properties": {"host": {"type": "string", "$ref": "#/x"}}},
        {"type": "object", "properties": {"host": {"type": "text"}}},
        {"type": "object", "properties": {"host": {"type": "string"}}, "required": ["port"]},
        {"type": "object", "properties": {"host": {"type": "string", "minimum": 1}}},
        {"type": "object", "properties": {"port": {"type": "integer", "default": "8080"}}},
        {"type": "object", "properties": {"port": {"type": "integer", "minimum": 9, "maximum": 1}}},
        {"type": "object", "properties": {"host": {"type": "string", "pattern": "["}}},
        {"type": "object", "properties": {"host": {"type": "string", "enum": [1, 2]}}},
        {"type": "object", "properties": {"host": {"type": "string"}}, "additionalProperties": "no"},
        {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "blob"}}}},
        {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}, "required": "tags"},
    ],
    ids=[
        "not_a_mapping",
        "root_type_is_not_object",
        "unknown_root_keyword",
        "unknown_field_keyword",
        "unsupported_field_type",
        "required_references_undeclared_field",
        "keyword_not_applicable_to_type",
        "default_violates_own_constraint",
        "minimum_greater_than_maximum",
        "pattern_is_not_a_valid_regex",
        "enum_conflicts_with_type",
        "additional_properties_is_not_boolean",
        "unsupported_item_type",
        "required_is_not_a_list",
    ],
)
def test_declaration_rejected_when_config_schema_is_malformed(config_schema):
    """契约本身畸形时整条声明被拒，而不是把契约字段悄悄忽略。"""
    declaration = ServiceInstanceDeclaration(
        capability="downloader",
        type="bad_schema_downloader",
        name="坏契约下载器",
        impl=_DemoClient,
        config_schema=config_schema,
    )

    violation = service_instance_declaration_violation(declaration)

    assert violation is not None
    plugin = _Plugin([declaration])
    assert PluginProjection({"BadSchemaPlugin": plugin}).provided_service_instances() == {
        "BadSchemaPlugin": []
    }


def test_declaration_rejected_when_schema_is_not_json_serializable():
    """契约含 JSON 往返后会变形的数据时被拒，跨进程握手要求它是纯 JSON。"""
    declaration = ServiceInstanceDeclaration(
        capability="downloader",
        type="tuple_schema_downloader",
        name="元组契约下载器",
        impl=_DemoClient,
        config_schema={
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ("push", "pull")}},
        },
    )

    violation = service_instance_declaration_violation(declaration)

    assert violation is not None
    assert "JSON" in violation


def test_valid_schema_round_trips_through_json():
    """合法契约 JSON 序列化往返后与原值相等，且往返件仍被判为合法。"""
    restored = json.loads(json.dumps(_VALID_SCHEMA))

    assert restored == _VALID_SCHEMA
    assert config_schema_violation(restored) is None


def test_impl_path_rejects_schema_declaring_host_filled_name():
    """impl 路径下契约不得声明 name：实例名由宿主填入，同名会撞掉构造。"""
    declaration = ServiceInstanceDeclaration(
        capability="downloader",
        type="name_schema_downloader",
        name="占名下载器",
        impl=_DemoClient,
        config_schema={"type": "object", "properties": {"name": {"type": "string"}}},
    )

    violation = service_instance_declaration_violation(declaration)

    assert violation is not None
    assert "name" in violation


def test_factory_path_may_declare_name_field():
    """factory 路径宿主不填实例名，契约声明 name 不构成冲突。"""
    declaration = ServiceInstanceDeclaration(
        capability="downloader",
        type="name_schema_factory",
        name="占名工厂",
        factory=lambda conf: _DemoClient(name=getattr(conf, "name", None)),
        config_schema={"type": "object", "properties": {"name": {"type": "string"}}},
    )

    assert service_instance_declaration_violation(declaration) is None


def test_declaration_without_schema_is_still_accepted():
    """未声明契约的类型照常通过契约校验，本步不把它判为违约。"""
    declaration = ServiceInstanceDeclaration(
        capability="downloader",
        type="legacy_downloader",
        name="无契约下载器",
        impl=_DemoClient,
    )

    assert service_instance_declaration_violation(declaration) is None


def test_declaration_without_schema_leaves_one_notice_per_extension(monkeypatch):
    """未声明契约的类型登记时按声明方提示一次，功能照常。"""
    deprecation_policy.reset_warned()
    log = _RecordingLogger()
    monkeypatch.setattr("app.runtime.deprecation.policy.logger", log)
    plugin = _Plugin(
        [
            ServiceInstanceDeclaration(
                capability="downloader",
                type="legacy_downloader",
                name="无契约下载器",
                impl=_DemoClient,
            )
        ]
    )
    projection = PluginProjection({"LegacyPlugin": plugin})

    accepted = projection.provided_service_instances()
    projection.provided_service_instances()

    assert len(accepted["LegacyPlugin"]) == 1
    assert len([item for item in log.warnings if "config_schema" in item]) == 1


def test_missing_schema_becomes_a_violation_once_deprecation_advances(monkeypatch):
    """废弃阶段推进到默认关闭后，同一处判据即把未声明契约的声明判为违约。"""
    disabled = replace(
        notices_module.NOTICES[SERVICE_INSTANCE_SCHEMA_DEPRECATION],
        stage=notices_module.DeprecationStage.DISABLED,
    )
    monkeypatch.setitem(
        notices_module.NOTICES, SERVICE_INSTANCE_SCHEMA_DEPRECATION, disabled
    )
    without_schema = ServiceInstanceDeclaration(
        capability="downloader",
        type="legacy_downloader",
        name="无契约下载器",
        impl=_DemoClient,
    )
    with_schema = ServiceInstanceDeclaration(
        capability="downloader",
        type="schema_downloader",
        name="带契约的下载器",
        impl=_DemoClient,
        config_schema=_VALID_SCHEMA,
    )

    violation = service_instance_declaration_violation(without_schema)

    assert violation is not None
    assert "config_schema" in violation
    assert service_instance_declaration_violation(with_schema) is None


# ---------------------------------------------------------------- 配置写入路径


def _register_demo_type(config_schema: Optional[Dict[str, Any]]) -> None:
    """在注册表里登记一个通知族类型。

    :param config_schema: 该类型声明的配置契约
    :return: 无返回值
    """
    service_instance_registry.register(
        capability=ModuleType.Notification.value,
        service_type="demo_channel",
        name="示例通道",
        owner="DemoPlugin",
        impl=_DemoClient,
        config_schema=config_schema,
    )


def test_write_path_rejects_config_violating_declared_schema():
    """写入路径拒绝畸形配置，原因指名实例、字段与实际取值。"""
    _register_demo_type(_VALID_SCHEMA)

    violation = service_config_write_violation(
        ModuleType.Notification.value,
        [_notification_config("我的通道", port=70000)],
    )

    assert violation is not None
    assert "我的通道" in violation
    assert "host 必填" in violation
    assert "port" in violation and "65535" in violation
    assert "ValidationError" not in violation and "pydantic" not in violation


def test_write_path_rejects_undeclared_field_and_nested_violation():
    """未声明字段与嵌套字段的违约同样被写入路径拦下，位置按点分路径给出。"""
    _register_demo_type(_VALID_SCHEMA)

    violation = service_config_write_violation(
        ModuleType.Notification.value,
        [_notification_config("我的通道", host="h", unknown=1, auth={"token": "ab"})],
    )

    assert violation is not None
    assert "unknown" in violation
    assert "auth.token" in violation


def test_write_path_accepts_config_matching_schema():
    """合契约的配置照常放行。"""
    _register_demo_type(_VALID_SCHEMA)

    assert service_config_write_violation(
        ModuleType.Notification.value,
        [
            _notification_config(
                "我的通道",
                host="h",
                port=8080,
                secure=True,
                mode="pull",
                tags=["a"],
                auth={"token": "abcd"},
            )
        ],
    ) is None


def test_write_path_ignores_types_without_schema():
    """未声明契约的类型不做形状判定，写入行为与本字段加入之前一致。"""
    _register_demo_type(None)

    assert service_config_write_violation(
        ModuleType.Notification.value,
        [_notification_config("我的通道", anything="whatever", port="not-a-number")],
    ) is None


def test_write_path_ignores_unregistered_types_and_other_keys():
    """未登记的类型与非服务族配置键都不产生判定。"""
    _register_demo_type(_VALID_SCHEMA)

    assert service_config_write_violation(
        ModuleType.Notification.value, [_notification_config("别的", "other_channel")]
    ) is None
    assert service_config_write_violation(
        service_capability(SystemConfigKey.Directories.value), ["anything"]
    ) is None
    assert service_config_write_violation(ModuleType.Notification.value, None) is None


def test_write_path_rejects_whole_write_and_lists_every_offender():
    """一条不合契约即退回整次写入，并逐条说明是哪个实例有问题。"""
    _register_demo_type(_VALID_SCHEMA)

    violation = service_config_write_violation(
        ModuleType.Notification.value,
        [
            _notification_config("合规的", host="h"),
            _notification_config("坏的甲", port="x"),
            _notification_config("坏的乙", host=1),
        ],
    )

    assert violation is not None
    assert "坏的甲" in violation and "坏的乙" in violation
    assert "合规的" not in violation


def test_setting_endpoint_rejects_malformed_service_config(monkeypatch):
    """设置写入端点退回畸形服务配置，不落盘、也不发配置变更事件。"""
    _register_demo_type(_VALID_SCHEMA)
    written = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.endpoints.system.async_write_system_setting", written)
    sent = AsyncMock()
    monkeypatch.setattr("app.api.endpoints.system.eventmanager.async_send_event", sent)

    response = asyncio.run(
        set_setting_endpoint(
            key=SystemConfigKey.Notifications.value,
            value=[_notification_config("我的通道", port=70000)],
        )
    )

    assert response.success is False
    assert "我的通道" in response.message
    written.assert_not_awaited()
    sent.assert_not_awaited()


def test_setting_endpoint_writes_config_without_declared_schema(monkeypatch):
    """未声明契约的类型照常写入，端点行为与本判定加入之前一致。"""
    _register_demo_type(None)
    written = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.endpoints.system.async_write_system_setting", written)
    monkeypatch.setattr(
        "app.api.endpoints.system.eventmanager.async_send_event", AsyncMock()
    )
    value = [_notification_config("我的通道", port="not-a-number")]

    response = asyncio.run(
        set_setting_endpoint(key=SystemConfigKey.Notifications.value, value=value)
    )

    assert response.success is True
    written.assert_awaited_once_with(SystemConfigKey.Notifications.value, value)


def test_agent_setting_tool_rejects_malformed_service_config():
    """智能体的系统设置工具与设置页写入同一道关卡。"""
    _register_demo_type(_VALID_SCHEMA)
    tool = UpdateSystemSettingsTool(session_id="session-1", user_id="10001")
    written = AsyncMock(return_value=True)

    with patch(
        "app.agent.tools.impl.update_system_settings.read_system_setting",
        return_value=[],
    ), patch(
        "app.agent.tools.impl.update_system_settings.async_write_system_setting",
        new=written,
    ), patch(
        "app.agent.tools.impl.update_system_settings.eventmanager.async_send_event",
        new=AsyncMock(),
    ):
        result = asyncio.run(
            tool.run(
                setting_key="Notifications",
                value=[_notification_config("我的通道", port=70000)],
            )
        )

    payload = json.loads(result)
    assert payload["success"] is False
    assert "我的通道" in payload["message"]
    written.assert_not_awaited()


# ---------------------------------------------------------------- 实例构造路径


def test_construction_rejects_config_violating_declared_schema():
    """构造路径按契约判定配置内容，畸形配置在构造前被拒。"""
    conf = NotificationConf(**_notification_config("我的通道", port=70000))

    with pytest.raises(ValueError) as error:
        create_service_instance(
            "我的通道", conf, impl=_DemoClient, config_schema=_VALID_SCHEMA
        )

    assert "host 必填" in str(error.value)


def test_construction_without_schema_matches_today():
    """未声明契约时构造路径不做形状判定，行为与本字段加入之前完全一致。"""
    conf = NotificationConf(**_notification_config("我的通道", anything="whatever"))

    instance = create_service_instance("我的通道", conf, impl=_DemoClient)

    assert instance.name == "我的通道"
    assert instance.config == {"anything": "whatever"}


def test_construction_error_isolation_is_preserved(
    service_configs: Dict[str, list], extension_logger: _RecordingLogger
):
    """一条不合契约的配置只跳过它自己，同类型其余实例照常产出。"""
    service_configs[SystemConfigKey.Notifications.value] = [
        _notification_config("合规的", host="h"),
        _notification_config("坏的", port=70000),
        _notification_config("另一个", host="h2"),
    ]

    instances = _adapter(_VALID_SCHEMA).get_instances()

    assert set(instances) == {"合规的", "另一个"}
    assert any("坏的" in message for message in extension_logger.errors)


def test_construction_without_schema_keeps_every_config(
    service_configs: Dict[str, list], extension_logger: _RecordingLogger
):
    """未声明契约时同一批配置全部产出实例，退化分叉不误伤合法配置。"""
    service_configs[SystemConfigKey.Notifications.value] = [
        _notification_config("合规的", host="h"),
        _notification_config("形状随意", port="not-a-number"),
    ]

    instances = _adapter(None).get_instances()

    assert set(instances) == {"合规的", "形状随意"}
    assert extension_logger.errors == []


# ---------------------------------------------------------------- 契约的对外下发


def test_config_form_endpoint_keeps_nested_schema_through_response_model():
    """契约随端点原样下发，response_model 不裁掉它的嵌套结构。"""
    _register_demo_type(_VALID_SCHEMA)

    result = service_config_form_endpoint(
        ModuleType.Notification.value, "demo_channel", None
    )

    assert result["config_schema"] == _VALID_SCHEMA
    serialized = ServiceConfigForm(**result).model_dump()
    assert serialized["config_schema"] == _VALID_SCHEMA
    assert serialized["config_schema"]["properties"]["auth"]["required"] == ["token"]
    assert serialized["available"] is False


def test_config_form_endpoint_reports_no_schema_when_undeclared():
    """未声明契约的类型下发 None，前端据此沿用内建渲染方式。"""
    _register_demo_type(None)

    result = service_config_form_endpoint(
        ModuleType.Notification.value, "demo_channel", None
    )

    assert result["config_schema"] is None
    assert ServiceConfigForm(**result).model_dump()["config_schema"] is None


# ---------------------------------------------------------------- 取值判定细节


@pytest.mark.parametrize(
    "value, expected",
    [
        ({"host": "h", "port": None}, ()),
        ({"host": None}, ("字段 host 必填，但未提供",)),
        ({"host": "h", "secure": 1}, ("字段 secure 应为布尔值，实际为整数 1",)),
        ({"host": "h", "mode": "sync"}, ("字段 mode 只能取 push、pull 之一，实际为字符串 'sync'",)),
        ({"host": "h", "tags": ["a", "b", "c", "d"]}, ("字段 tags 最多只能有 3 项",)),
        ({"host": "h", "tags": [1]}, ("字段 tags[0] 应为字符串，实际为整数 1",)),
        ({"host": "", "port": 8080}, ("字段 host 长度不能少于 1 个字符",)),
    ],
    ids=[
        "none_counts_as_absent",
        "required_field_is_none",
        "boolean_is_not_an_integer",
        "enum_membership",
        "array_length",
        "array_item_type",
        "string_length",
    ],
)
def test_config_value_violations_are_readable(value, expected):
    """取值判定按字段逐条给出中文原因，None 一律视为未提供。"""
    assert config_value_violations(_VALID_SCHEMA, value) == expected


def test_config_value_violations_without_schema_are_empty():
    """未声明契约时不产生任何判定。"""
    assert config_value_violations(None, {"anything": object()}) == ()
