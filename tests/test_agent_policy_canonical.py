import math
from dataclasses import FrozenInstanceError

import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.agent.policy.canonical import (
    CanonicalizationError,
    canonicalize_invocation,
)
from app.agent.policy.contracts import (
    ConversationKind,
    DeliveryTarget,
    InboundEnvelope,
    InboundProvenance,
    ToolRevision,
)
from app.agent.policy.registry import DEFAULT_TOOL_POLICY_REGISTRY
from app.agent.tools.impl.query_system_settings import QuerySystemSettingsTool


def _secret_policy():
    return DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="query_system_settings",
        arguments={"show_secrets": True},
        requires_admin=True,
    )


def _tool_revision() -> ToolRevision:
    return ToolRevision(
        implementation="query-system-settings:1",
        factory="builtin-factory:1",
        plugin="plugin-catalog:1",
    )


def test_canonical_invocation_includes_defaults_and_is_stable() -> None:
    """省略的默认值必须进入相同规范化参数与摘要。"""
    tool = QuerySystemSettingsTool(session_id="session-1", user_id="admin")

    omitted = canonicalize_invocation(
        tool=tool,
        arguments={"setting_key": "COOKIECLOUD_KEY", "show_secrets": True},
        policy=_secret_policy(),
        tool_revision=_tool_revision(),
    )
    explicit = canonicalize_invocation(
        tool=tool,
        arguments={
            "setting_key": "COOKIECLOUD_KEY",
            "group": "all",
            "keyword": None,
            "include_values": None,
            "show_secrets": True,
        },
        policy=_secret_policy(),
        tool_revision=_tool_revision(),
    )

    assert omitted.digest == explicit.digest
    assert omitted.arguments == explicit.arguments
    assert omitted.preconditions == (
        ("setting", "settings:COOKIECLOUD_KEY:settings"),
    )


def test_canonical_invocation_repr_hides_arguments_and_json() -> None:
    """调用对象的 repr 不得泄露确认参数或完整规范化 JSON。"""
    marker = "canonical-private-marker"

    class _Input(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: str

    class _Tool:
        name = "private_tool"
        args_schema = _Input

    invocation = canonicalize_invocation(
        tool=_Tool(),
        arguments={"value": marker},
        policy=_secret_policy(),
        tool_revision=_tool_revision(),
    )

    assert marker not in repr(invocation)
    assert "canonical_json" not in repr(invocation)


def test_canonical_arguments_are_recursively_immutable() -> None:
    """确认等待期间不能修改嵌套参数后复用旧 digest。"""

    class _Input(BaseModel):
        payload: dict[str, list[str]]

    class _Tool:
        name = "nested_tool"
        args_schema = _Input

    invocation = canonicalize_invocation(
        tool=_Tool(),
        arguments={"payload": {"items": ["one"]}},
        policy=_secret_policy(),
        tool_revision=_tool_revision(),
    )

    with pytest.raises(TypeError):
        invocation.arguments["payload"]["items"] += ("two",)


def test_inbound_contract_hides_raw_text_and_keeps_target_identities_separate() -> None:
    """入站原文不可进入 repr，actor、recipient 和 conversation 独立绑定。"""
    target = DeliveryTarget(
        channel="telegram",
        source_instance_id="source-1",
        tenant_or_account_id="account-1",
        conversation_kind=ConversationKind.PRIVATE,
        conversation_id="chat-1",
        recipient_id="recipient-1",
        actor_id="actor-1",
        server_session_id="session-1",
    )
    envelope = InboundEnvelope(
        provenance=InboundProvenance.VERIFIED_ADAPTER,
        target=target,
        inbound_event_id="event-1",
        raw_text="  confirm K7P4-M2Q8  ",
        normalized_text="confirm K7P4-M2Q8",
    )

    rendered = repr(envelope)
    assert "K7P4-M2Q8" not in rendered
    assert target.actor_id != target.recipient_id
    assert target.recipient_id != target.conversation_id
    with pytest.raises(FrozenInstanceError):
        envelope.inbound_event_id = "forged"


def test_canonical_invocation_rejects_unknown_arguments() -> None:
    """严格确认不能把 schema 外参数降级为原始字典。"""
    tool = QuerySystemSettingsTool(session_id="session-1", user_id="admin")

    with pytest.raises(CanonicalizationError, match="参数校验失败"):
        canonicalize_invocation(
            tool=tool,
            arguments={"show_secrets": True, "forged": "value"},
            policy=_secret_policy(),
            tool_revision=_tool_revision(),
        )


def test_canonical_invocation_rejects_missing_pydantic_schema() -> None:
    """动态 dict schema 不能进入严格确认路径。"""

    class _Tool:
        name = "dynamic_tool"
        args_schema = {"type": "object"}

    with pytest.raises(CanonicalizationError, match="Pydantic"):
        canonicalize_invocation(
            tool=_Tool(),
            arguments={},
            policy=_secret_policy(),
            tool_revision=_tool_revision(),
        )


def test_canonical_invocation_preserves_unicode_and_rejects_nan() -> None:
    """稳定 JSON 保留 Unicode，同时禁止非标准 NaN。"""

    class _Input(BaseModel):
        label: str
        value: float = Field(allow_inf_nan=True)

    class _Tool:
        name = "unicode_tool"
        args_schema = _Input

    with pytest.raises(CanonicalizationError, match="无法规范化"):
        canonicalize_invocation(
            tool=_Tool(),
            arguments={"label": "中文", "value": math.nan},
            policy=_secret_policy(),
            tool_revision=_tool_revision(),
        )


def test_canonicalization_reads_no_setting_value(monkeypatch) -> None:
    """确认前只能读取静态 SettingSpec，不能访问实际设置值。"""
    tool = QuerySystemSettingsTool(session_id="session-1", user_id="admin")
    load_value = monkeypatch.setattr(
        QuerySystemSettingsTool,
        "_load_setting_value",
        lambda *_: pytest.fail("canonicalization must not read setting value"),
    )

    invocation = canonicalize_invocation(
        tool=tool,
        arguments={"setting_key": "COOKIECLOUD_KEY", "show_secrets": True},
        policy=_secret_policy(),
        tool_revision=_tool_revision(),
    )

    assert invocation.digest
    assert load_value is None


def test_group_selector_binds_static_setting_set_without_loading_values(
    monkeypatch,
) -> None:
    """列表型密钥读取必须绑定匹配的静态设置集合。"""
    tool = QuerySystemSettingsTool(session_id="session-1", user_id="admin")
    monkeypatch.setattr(
        QuerySystemSettingsTool,
        "_load_setting_value",
        lambda *_: pytest.fail("canonicalization must not read setting value"),
    )

    invocation = canonicalize_invocation(
        tool=tool,
        arguments={"group": "ai_agent", "show_secrets": True},
        policy=_secret_policy(),
        tool_revision=_tool_revision(),
    )

    assert len(invocation.preconditions) > 1
    assert all(kind == "setting" for kind, _identity in invocation.preconditions)
