"""缓存载荷签名编解码与 Redis 序列化格式的契约测试。"""

import json
import pickle
from dataclasses import dataclass
from unittest.mock import Mock

import pytest

from app.adapters.cache import redis as redis_module
from app.adapters.cache.redis import RedisHelper, deserialize, serialize
from app.application.chain.context import ChainRuntimeContext
from app.chain.base import ChainBase
from app.runtime.cache import SIGNED_PICKLE_MARKER, SignedPickleCodec
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher


@dataclass
class _Payload:
    """JSON 无法直接表达的 dataclass 载荷。"""

    name: str
    ids: set[int]


@pytest.fixture
def codec() -> SignedPickleCodec:
    """固定密钥的编解码器。"""
    return SignedPickleCodec(lambda: "unit-test-secret")


def test_codec_roundtrip_and_marker(codec: SignedPickleCodec) -> None:
    """签名载荷可往返，且带可识别前缀。"""
    value = _Payload(name="movie", ids={1, 2})

    data = codec.dumps(value)

    assert data.startswith(SIGNED_PICKLE_MARKER + b"\x00")
    assert SignedPickleCodec.is_signed(data)
    assert codec.loads(data) == value


def test_codec_rejects_tampered_payload(codec: SignedPickleCodec) -> None:
    """篡改任意一个字节都应被签名校验拒绝。"""
    data = bytearray(codec.dumps({"a": 1}))
    data[-1] ^= 0x01

    with pytest.raises(ValueError, match="签名无效"):
        codec.loads(bytes(data))


def test_codec_rejects_legacy_unsigned_pickle(codec: SignedPickleCodec) -> None:
    """历史未签名 pickle 与裸 pickle 字节一律拒绝。"""
    with pytest.raises(ValueError, match="未签名"):
        codec.loads(b"PICKLE\x00" + pickle.dumps({"a": 1}))
    with pytest.raises(ValueError, match="未签名"):
        codec.loads(pickle.dumps({"a": 1}))


def test_codec_rejects_payload_signed_with_another_key(codec: SignedPickleCodec) -> None:
    """密钥轮换后旧载荷失效。"""
    other = SignedPickleCodec(lambda: "rotated-secret")

    with pytest.raises(ValueError, match="签名无效"):
        other.loads(codec.dumps({"a": 1}))


def test_redis_serialize_prefers_json_and_passes_bytes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON 可表达的值走 JSON，字节原样透传，不可 JSON 的值走签名 pickle。"""
    monkeypatch.setattr(redis_module, "_signed_pickle", SignedPickleCodec(lambda: "redis-secret"))

    json_blob = serialize({"a": [1, 2]})
    assert json_blob == b"JSON\x00" + json.dumps({"a": [1, 2]}).encode("utf-8")
    assert deserialize(json_blob) == {"a": [1, 2]}

    raw = serialize(b"\x80\x05already-encoded")
    assert raw == b"BYTES\x00\x80\x05already-encoded"
    assert deserialize(raw) == b"\x80\x05already-encoded"

    signed = serialize(_Payload(name="x", ids={3}))
    assert SignedPickleCodec.is_signed(signed)
    assert deserialize(signed) == _Payload(name="x", ids={3})


def test_redis_deserialize_rejects_legacy_and_unknown_formats() -> None:
    """旧 PICKLE 标记与未知标记都抛 ValueError，由调用方按未命中处理。"""
    with pytest.raises(ValueError):
        deserialize(b"PICKLE\x00" + pickle.dumps({"a": 1}))
    with pytest.raises(ValueError):
        deserialize(b"WHATEVER\x00data")


def test_redis_items_skip_bad_payloads_and_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    """遍历时单个坏载荷被跳过，其余键继续产出。"""
    monkeypatch.setattr(redis_module, "_signed_pickle", SignedPickleCodec(lambda: "redis-secret"))
    store = {
        b"region:DEFAULT:key:good": serialize({"ok": True}),
        b"region:DEFAULT:key:legacy": b"PICKLE\x00" + pickle.dumps({"evil": True}),
        b"region:DEFAULT:key:bytes": serialize(b"blob"),
    }

    class FakeClient:
        """最小的 scan_iter/get 客户端。"""

        def scan_iter(self, pattern):
            """按插入顺序返回全部键。"""
            assert pattern == "region:DEFAULT:key:*"
            return iter(store.keys())

        def get(self, key):
            """返回存储的字节。"""
            return store[key]

    # 在类上打桩而不是实例：实例级 monkeypatch 还原时会把绑定方法写回实例属性，
    # 遮蔽后续用例在类上打的桩，导致单例去连真实 Redis。
    monkeypatch.setattr(RedisHelper, "_connect", lambda _self: None)
    helper = RedisHelper()
    helper.client = FakeClient()
    try:
        items = dict(helper.items(region="DEFAULT"))
    finally:
        helper.client = None

    assert items == {"good": {"ok": True}, "bytes": b"blob"}


def _chain_with_codec(file_cache: Mock, cache_codec) -> ChainBase:
    """构造只注入文件缓存与编解码器的最小 Chain。"""
    message_queue = Mock()
    message_queue.bind.return_value = Mock()
    module_manager = Mock()
    module_manager.get_running_modules.return_value = []
    plugin_manager = Mock()
    plugin_manager.get_plugin_modules.return_value = {}
    return ChainBase(
        ChainRuntimeContext(
            module_manager=module_manager,
            plugin_manager=plugin_manager,
            event_manager=Mock(),
            message_oper=Mock(),
            message_helper=Mock(),
            file_cache=file_cache,
            async_file_cache=Mock(),
            cache_codec=cache_codec,
            message_queue=message_queue,
            module_dispatcher_factory=ModuleInvocationDispatcher,
            site_repository=Mock(),
            subscription_repository=Mock(),
            subscription_mutation_scope=Mock(),
            sync_subscription_mutation_scope=Mock(),
            subscription_delete_scope=Mock(),
            sync_subscription_delete_scope=Mock(),
            subscription_completion_scope=Mock(),
            rule_group_mutation_scope=Mock(),
            site_reference_mutation_scope=Mock(),
            download_history_repository=Mock(),
            transfer_history_repository=Mock(),
            transfer_admission_repository=Mock(),
            transfer_execution_repository=Mock(),
            media_server_repository=Mock(),
            download_failure_repository=Mock(),
            user_repository=Mock(),
        )
    )


def test_chain_cache_roundtrips_through_signed_codec(codec: SignedPickleCodec) -> None:
    """Chain 保存的缓存经签名编解码器写入文件缓存，并能原样读回。"""
    store: dict[str, bytes] = {}
    file_cache = Mock()
    file_cache.set.side_effect = lambda key, value: store.__setitem__(key, value)
    file_cache.get.side_effect = lambda key: store.get(key)
    chain = _chain_with_codec(file_cache, codec)

    chain.save_cache({"__search_params__": {"keyword": "movie"}}, "__search_params__")

    assert SignedPickleCodec.is_signed(store["__search_params__"])
    assert chain.load_cache("__search_params__") == {"__search_params__": {"keyword": "movie"}}


def test_chain_cache_treats_unsigned_or_missing_codec_as_miss(codec: SignedPickleCodec) -> None:
    """旧版未签名载荷与未装配编解码器都按未命中处理，不抛异常也不写入。"""
    file_cache = Mock()
    file_cache.get.return_value = pickle.dumps({"stale": True})

    assert _chain_with_codec(file_cache, codec).load_cache("__search_result__") is None

    chain = _chain_with_codec(file_cache, None)
    assert chain.load_cache("__search_result__") is None
    chain.save_cache({"x": 1}, "__search_result__")
    file_cache.set.assert_not_called()
