"""
扩展传输标识的无状态解析、持久化边界和公开序列化回归。
"""

import json

import pytest
from pydantic import TypeAdapter

from app.schemas.types import NotificationChannel


def test_uncached_transport_preserves_enum_equality_hashing_and_builtin_identity():
    """
    无缓存扩展仍可比较和充当键，复制与序列化不改变值；内置成员继续保持单例。
    """
    import copy
    import pickle

    from pydantic import TypeAdapter

    first = NotificationChannel("platform:uncached-equality")
    second = NotificationChannel("platform:uncached-equality")
    different = NotificationChannel("platform:uncached-other")
    adapter = TypeAdapter(NotificationChannel)
    assert first == second and first != different
    assert hash(first) == hash(second)
    assert {first: "owner"}[second] == "owner"
    assert len({first, second, different}) == 2
    assert first != first.value and first != object()
    assert first not in {NotificationChannel.Web, NotificationChannel.WebAgent}
    assert copy.copy(first) == first
    assert copy.deepcopy(first) == first
    assert pickle.loads(pickle.dumps(first)) == first
    assert adapter.validate_json(adapter.dump_json(first)) == first
    for member in NotificationChannel:
        assert NotificationChannel(member.value) is member
        assert NotificationChannel[member.name] is member
        assert adapter.validate_json(adapter.dump_json(member)) is member
        assert hash(member) == hash(member.name)
        assert member != member.value


def test_public_transport_schema_preserves_builtin_values_and_namespaced_extensions():
    """
    公开接口的 Schema 同时发布内置值和扩展格式，编码往返不能丢失独立平台身份。
    """
    import re

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.schemas.message import IncomingMessage

    app = FastAPI()

    @app.post("/message", response_model=IncomingMessage)
    def echo_message(message: IncomingMessage) -> IncomingMessage:
        """
        保留真实公开消息 DTO 的编解码，不接入授权或外部传输。
        """
        return message

    schema = app.openapi()["components"]["schemas"]["NotificationChannel"]
    builtins, extension = schema["anyOf"]
    assert set(builtins["enum"]) == {member.value for member in NotificationChannel}
    assert "enum" not in schema
    pattern = extension["pattern"]
    maximum = "platform:" + "a" * 55
    assert len(maximum) == 64 and re.fullmatch(pattern, maximum)
    assert not re.fullmatch(pattern, maximum + "a")
    assert not re.fullmatch(pattern, "platform:Bad")
    with TestClient(app) as client:
        for channel in ("Telegram", "platform:independent-fixture", maximum):
            response = client.post("/message", json={"channel": channel, "userid": "p-alice"})
            assert response.status_code == 200, response.text
            assert response.json()["channel"] == channel
        assert client.post("/message", json={"channel": "unknown", "userid": "p-alice"}).status_code == 422
    assert all(member.value != maximum for member in NotificationChannel)
    with pytest.raises(ValueError):
        NotificationChannel(123)


def test_unknown_transport_batch_keeps_enum_registry_bounded():
    """
    解析和序列化大量未注册标识不能修改进程级枚举成员表。
    """
    values_before = dict(NotificationChannel._value2member_map_)
    members_before = dict(NotificationChannel.__members__)
    names_before = tuple(NotificationChannel._member_names_)
    adapter = TypeAdapter(NotificationChannel)
    for index in range(4096):
        identifier = f"platform:unregistered-batch-{index}"
        channel = NotificationChannel(identifier)
        decoded = adapter.validate_json(json.dumps(identifier))
        assert channel == decoded
        assert adapter.dump_json(decoded) == json.dumps(identifier).encode()
    assert NotificationChannel._value2member_map_ == values_before
    assert dict(NotificationChannel.__members__) == members_before
    assert tuple(NotificationChannel._member_names_) == names_before


@pytest.mark.parametrize("identifier", ["platform:", "platform:Bad", "platform:" + "a" * 56, "platform:with space"])
def test_extension_transport_identifiers_fit_persistent_contract(identifier):
    """
    扩展传输格式与现有 64 字符持久化限制一致，不悄悄扩大数据库合同。
    """
    with pytest.raises(ValueError):
        NotificationChannel(identifier)
