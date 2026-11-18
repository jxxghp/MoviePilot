"""
使用真实宿主 HTTP 适配链和合成传输验证知识协议、默认关闭及配置变化。
"""

import asyncio
import json
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest
from pydantic import ValidationError

from app.adapters.external.knowledge import MAX_RESPONSE_BYTES, KnowledgeClient
from app.adapters.network.http import AsyncRequestUtils
from app.application.configuration import SystemConfigWriteResult
from app.application.knowledge import KnowledgeService, normalize_knowledge_config
from app.application.security.secrets import is_secret_setting_key
from app.application.settings.contract import ALL_SETTING_SPECS, validate_setting_value
from app.application.settings.service import SystemSettingsService, redact_setting_value
from app.schemas.knowledge import AgentKnowledgeConfig, KnowledgeOutcome, KnowledgeRead, KnowledgeSearch
from app.schemas.types import SystemConfigKey

ORIGIN = "https://knowledge.example.invalid"
TOKEN = "mpk_" + "s" * 43
REFERENCE = "ke_" + "r" * 43
SENTINEL = "本轮合成知识正文，不得输出到默认对象展示"


def _config(**changes):
    """
    构造不对应任何真实中心或用户的完整配置。

    :param changes: 本用例需要替换的配置字段
    :return: 可供服务快照读取的合成字典
    """
    return {"enabled": True, "origin": ORIGIN, "token": TOKEN, **changes}


def _reply(status="matched"):
    """
    对照当前中心实际协议构造完整证据，资料全部为合成值。

    :param status: 需要验证的中心结果状态
    :return: JSON 可序列化的中心响应
    """
    item = {
        "document_id": "a" * 32, "revision": 2, "title": "合成配置规则", "excerpt": SENTINEL,
        "source_label": "合成来源", "collection_label": "合成集合", "source_url": None,
        "source_commit": None, "domain": "mp", "product": "moviepilot", "site_id": None,
        "applicable_versions": {"scope": "unknown", "min": None, "max": None},
        "fetched_at": "2026-11-19T00:00:00+00:00", "valid_until": None,
        "freshness": "dated", "stale": None, "kind": "document", "section": "chunk:1",
        "evidence_id": REFERENCE,
    }
    return {
        "protocol_version": 1, "request_id": "b" * 32, "corpus_revision": 1, "status": status,
        "evidence": [item] if status in ("matched", "conflicting") else [], "notices": ["applicability_unknown"],
        "retention": "ephemeral", "content_trust": "reference_only", "reference_seconds": 600,
    }


def _requester(client):
    """
    装配生产使用的网络层配置到仅服务于本用例的 MockTransport 客户端。

    :param client: 由测试管理的合成传输客户端
    :return: 不跟随跳转、验证 TLS 且禁用环境代理继承的宿主请求器
    """
    return AsyncRequestUtils(client=client, http2=False, timeout=3, verify=True,
                             follow_redirects=False, trust_env=False)
@pytest.mark.parametrize("origin", [
    "http://knowledge.example.invalid", "https://user:password@knowledge.example.invalid",
    ORIGIN + "/path", ORIGIN + "?token=x", ORIGIN + "#fragment", ORIGIN + "?",
    ORIGIN + ":bad", ORIGIN + "\n", "https://knowledge.example.invalid\\@other.invalid",
])
def test_config_rejects_ambiguous_credential_targets(origin):
    """
    凭据不能发给模型自由指定的路径、非 TLS 源或解析含糊的目标。
    """
    with pytest.raises(ValidationError) as error:
        AgentKnowledgeConfig.model_validate(_config(origin=origin))
    assert TOKEN not in str(error.value)


@pytest.mark.parametrize("changes", [{"token": "***"}, {"token": "Bearer x"}, {"token": TOKEN + "\r\n"},
                                         {"token": ""}, {"origin": ""}, {"enabled": "true"}, {"extra": "x"}])
def test_config_is_complete_strict_and_default_off(changes):
    """
    启用需要完整独立凭据，拒绝回填脱敏值及隐式类型转换。
    """
    assert AgentKnowledgeConfig().enabled is False
    assert AgentKnowledgeConfig(origin=ORIGIN + "/").origin == ORIGIN
    with pytest.raises(ValidationError) as error:
        AgentKnowledgeConfig.model_validate(_config(**changes))
    assert TOKEN not in str(error.value)


def test_setting_contract_replaces_and_redacts_the_whole_binding():
    """
    设置发现保留 Schema，但当前值整体受保护且不允许局部修改中心地址。
    """
    spec = ALL_SETTING_SPECS[SystemConfigKey.AIAgentKnowledge.value]
    assert spec.update_operations == ("replace",)
    assert spec.sensitive and is_secret_setting_key(spec.key)
    parsed = validate_setting_value(spec, _config())
    assert parsed.token == TOKEN and TOKEN not in repr(parsed) and ORIGIN not in repr(parsed)
    assert redact_setting_value(spec, _config()) == {"enabled": True, "origin": "***", "token": "***"}
    with pytest.raises(ValidationError):
        validate_setting_value(spec, {"enabled": True, "origin": ORIGIN, "token": "***"})


@pytest.mark.parametrize("show_secrets", [False, True])
def test_system_settings_apis_never_reveal_center_binding(show_secrets):
    """
    即使其他设置允许明文读取，Agent 专用中心绑定也只能更新、不能回读。
    """
    system = MagicMock()
    system.get.side_effect = lambda key: _config() if key == SystemConfigKey.AIAgentKnowledge else None
    runtime_settings = MagicMock()
    runtime_settings.get.return_value = None
    service = SystemSettingsService(runtime_settings, system, AsyncMock())
    result = service.describe(setting_key="AIAgentKnowledge", show_secrets=show_secrets)
    assert result["redacted"] is True
    assert result["value"] == {"enabled": True, "origin": "***", "token": "***"}
    assert TOKEN not in json.dumps(result) and ORIGIN not in json.dumps(result)
    batch_result = service.query(group="ai_agent", include_values=True, show_secrets=True)
    batch_value = next(item["value"] for item in batch_result["settings"]
                       if item["setting_key"] == "AIAgentKnowledge")
    assert batch_value == {"enabled": True, "origin": "***", "token": "***"}
    assert TOKEN not in json.dumps(batch_result) and ORIGIN not in json.dumps(batch_result)
    query_result = service.query(setting_key="AIAgentKnowledge", include_values=True, show_secrets=True)
    assert query_result["settings"][0]["value"] == {"enabled": True, "origin": "***", "token": "***"}


@pytest.mark.parametrize("key", [SystemConfigKey.AIAgentKnowledge, "AIAgentKnowledge"])
def test_all_configuration_write_paths_normalize_or_fail_closed(key):
    """
    绕过设置目录的原始配置写入仍执行同一地址和令牌合同。
    """
    assert normalize_knowledge_config(key, _config(origin=ORIGIN + "/")) == _config()
    assert normalize_knowledge_config(key, None) is None
    assert normalize_knowledge_config(SystemConfigKey.AIAgentConfig, {"token": "ordinary"}) == {"token": "ordinary"}
    with pytest.raises(ValidationError) as error:
        normalize_knowledge_config(key, _config(token="***"))
    assert TOKEN not in str(error.value)


@pytest.mark.asyncio
async def test_system_setting_update_validates_normalizes_and_redacts_saved_binding(monkeypatch):
    """
    设置 API 只接受完整中心绑定，系统写端规范化后回执仍不回显秘密。
    """
    import app.application.settings as settings_module

    saved: dict[str, object] = {"enabled": False}
    system = MagicMock()

    def get_value(_key):
        """
        读取本用例的隔离配置副本。

        :param _key: 待读取的合成设置键
        :return: 当前持久化合成值
        """
        return deepcopy(saved)

    async def save_value(key, value):
        """
        模拟 SystemConfigService 对候选值执行的最终规范化写入。

        :param key: 已登记的系统配置键
        :param value: 已由设置合同验证的完整候选值
        :return: 规范化值和成功写入结果
        """
        normalized = normalize_knowledge_config(key, value)
        saved.clear()
        saved.update(normalized)
        return SystemConfigWriteResult(changed=True, normalized_value=normalized)

    system.get.side_effect = get_value
    system.async_set_with_normalized_value = AsyncMock(side_effect=save_value)
    publish = AsyncMock()
    service = SystemSettingsService(MagicMock(), system, publish)
    monkeypatch.setattr(settings_module, "plugin_system_config_mutation", lambda _key: nullcontext())
    result = await service.update(setting_key="AIAgentKnowledge", value=_config(origin=ORIGIN + "/"))
    assert saved == _config()
    assert result["changed"] is True and result["values_redacted"] is True
    assert result["saved_value"] == {"enabled": True, "origin": "***", "token": "***"}
    assert TOKEN not in json.dumps(result) and ORIGIN not in json.dumps(result)
    publish.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "read"])
async def test_real_transport_uses_fixed_origin_independent_auth_and_no_replay(operation):
    """
    真实 AsyncRequestUtils 只向固定 HTTPS 路径发一个 POST，中心引用不在 URL 中。
    """
    observed = []

    def handle(request):
        """
        记录实际进入 MockTransport 的请求，不访问网络。

        :param request: 网络层构造的实际 HTTP 请求
        :return: 合成中心返回
        """
        observed.append(request)
        return httpx2.Response(200, json=_reply())

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        client = KnowledgeClient(_requester(http))
        payload = KnowledgeSearch(query="合成规则") if operation == "search" else KnowledgeRead(evidence_id=REFERENCE)
        result = await client.query(AgentKnowledgeConfig(**_config()), payload)
    assert result.status == "matched" and result.response.evidence[0].excerpt == SENTINEL
    assert SENTINEL not in repr(result) and REFERENCE not in repr(result)
    assert len(observed) == 1
    request = observed[0]
    assert str(request.url) == ORIGIN + "/knowledge/v1/" + operation
    assert request.method == "POST" and request.headers["authorization"] == "Bearer " + TOKEN
    assert "cookie" not in request.headers and "x-api-key" not in request.headers
    assert request.headers["accept-encoding"] == "identity"
    body = json.loads(request.content)
    assert "token" not in body and "origin" not in body and body["protocol_version"] == 1
    assert TOKEN.encode() not in request.content


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "status"), [
    (401, "unauthorized"), (403, "forbidden"), (410, "reference_expired"), (413, "invalid_request"),
    (415, "invalid_request"), (422, "invalid_request"), (429, "rate_limited"), (503, "unavailable"),
    (500, "unavailable"), (302, "unavailable"), (307, "unavailable"),
])
async def test_failures_do_not_become_no_match_or_follow_redirects(code, status):
    """
    外部错误正文和跳转目标不会被执行或作为没查到知识返回。
    """
    observed = []

    def handle(request):
        """
        返回错误或跨源跳转并记录调用数。

        :param request: 实际请求
        :return: 带不可信错误正文的 HTTP 响应
        """
        observed.append(request)
        return httpx2.Response(code, text=SENTINEL, headers={"Location": "https://other.invalid/path"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle), follow_redirects=True) as http:
        result = await KnowledgeClient(_requester(http)).query(AgentKnowledgeConfig(**_config()), KnowledgeSearch(query="规则"))
    assert result == KnowledgeOutcome(status) and SENTINEL not in repr(result)
    assert len(observed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["matched", "no_match", "not_applicable", "conflicting"])
async def test_protocol_preserves_authoritative_result_states(status):
    """
    中心命中、不适用和冲突保持原义，未来附加字段不破坏当前投影。
    """
    data = {**_reply(status), "future_optional_field": True}
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _request: httpx2.Response(200, json=data))) as http:
        result = await KnowledgeClient(_requester(http)).query(AgentKnowledgeConfig(**_config()), KnowledgeSearch(query="规则"))
    assert result.status == status and result.response.notices == ["applicability_unknown"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["retention", "trust", "protocol", "schema", "status", "snippet", "count", "total", "reference"])
async def test_untrusted_response_must_satisfy_protocol_and_budgets(mutation):
    """
    协议漂移、状态矛盾、引用损坏和超预算均失败关闭，不静默删减资料。
    """
    data = deepcopy(_reply())
    if mutation == "retention":
        data["retention"] = "persistent"
    elif mutation == "trust":
        data["content_trust"] = "instructions"
    elif mutation == "protocol":
        data["protocol_version"] = 2
    elif mutation == "schema":
        data["evidence"][0]["revision"] = True
    elif mutation == "status":
        data["status"] = "no_match"
    elif mutation == "snippet":
        data["evidence"][0]["excerpt"] = "长" * 1601
    elif mutation == "count":
        data["evidence"] *= 4
    elif mutation == "total":
        data["evidence"][0]["excerpt"] = "长" * 3100
        data["evidence"] *= 2
    else:
        data["evidence"][0]["evidence_id"] = "invalid"
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _request: httpx2.Response(200, json=data))) as http:
        result = await KnowledgeClient(_requester(http)).query(AgentKnowledgeConfig(**_config()), KnowledgeSearch(query="规则"))
    assert result == KnowledgeOutcome("invalid_response")


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx2.Response(200, content=b"not json", headers={"Content-Type": "application/json"}),
    httpx2.Response(200, json=_reply(), headers={"Content-Type": "text/html"}),
    httpx2.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1), headers={"Content-Type": "application/json"}),
])
async def test_invalid_encoding_type_or_buffered_size_is_rejected(response):
    """
    完成缓冲后仍检查类型、JSON 和字节上限，不把 HTML 登录页当资料。
    """
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _request: response)) as http:
        result = await KnowledgeClient(_requester(http)).query(AgentKnowledgeConfig(**_config()), KnowledgeSearch(query="规则"))
    assert result == KnowledgeOutcome("invalid_response") and response.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["connect", "timeout"])
async def test_transport_failures_are_sanitized_and_not_retried(kind):
    """
    传输异常不回显可能含凭据的异常文本，POST 失败也不自动重放。
    """
    calls = []

    def handle(request):
        """
        模拟带私有哨兵的网络错误。

        :param request: 当前固定目标的请求
        :return: 始终抛出合成网络错误
        """
        calls.append(request)
        error = httpx2.ReadTimeout if kind == "timeout" else httpx2.ConnectError
        raise error(TOKEN + SENTINEL, request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        result = await KnowledgeClient(_requester(http)).query(AgentKnowledgeConfig(**_config()), KnowledgeSearch(query="规则"))
    assert result == KnowledgeOutcome("unavailable")
    assert len(calls) == 1 and TOKEN not in repr(result) and SENTINEL not in repr(result)


@pytest.mark.asyncio
async def test_read_accepts_only_the_requested_reference_and_one_context():
    """
    补读不能借中心错误返回另一引用或多个全文窗口。
    """
    data = _reply()
    data["evidence"][0]["evidence_id"] = "ke_" + "x" * 43
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _request: httpx2.Response(200, json=data))) as http:
        result = await KnowledgeClient(_requester(http)).query(AgentKnowledgeConfig(**_config()), KnowledgeRead(evidence_id=REFERENCE))
    assert result.status == "invalid_response"
    data = _reply()
    data["evidence"] *= 2
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _request: httpx2.Response(200, json=data))) as http:
        result = await KnowledgeClient(_requester(http)).query(AgentKnowledgeConfig(**_config()), KnowledgeRead(evidence_id=REFERENCE))
    assert result.status == "invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, {}, {"enabled": False}, _config(token="***"), _config(enabled=False)])
async def test_unconfigured_or_disabled_service_never_calls_transport(value):
    """
    默认关闭、脱敏占位或损坏的旧配置都不能触发访问。
    """
    config, port = MagicMock(), MagicMock(query=AsyncMock())
    config.get.return_value = value
    service = KnowledgeService(config, port, lambda: "v3.2.5")
    assert service.signature() == "disabled"
    assert await service.search("规则") == KnowledgeOutcome("disabled")
    assert await service.read(REFERENCE) == KnowledgeOutcome("disabled")
    port.query.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("host_version", "wire_version"), [("v3.2.5", "3.2.5"), ("3.2.5", "3.2.5"),
                                                            ("v3", None), ("3.2.5-dev", None), (None, None)])
async def test_service_sends_only_short_query_host_version_and_fixed_budget(host_version, wire_version):
    """
    产品版本只能来自宿主，开发分支不冒充稳定版，条数不是模型参数。
    """
    config, port = MagicMock(), MagicMock(query=AsyncMock(return_value=KnowledgeOutcome("no_match")))
    config.get.return_value = _config()
    service = KnowledgeService(config, port, lambda: host_version)
    result = await service.search("  合成规则  ", domain="pt", site="合成站", purpose="troubleshooting")
    assert result.status == "no_match"
    binding, request = port.query.await_args.args
    assert binding.origin == ORIGIN and binding.token == TOKEN
    assert request.query == "合成规则" and request.product == "moviepilot"
    assert request.product_version == wire_version and request.limit == 3 and request.site == "合成站"
    assert request.domain == "pt" and request.purpose == "troubleshooting"
    config.get.assert_called_with(SystemConfigKey.AIAgentKnowledge)


@pytest.mark.asyncio
async def test_revocation_during_request_drops_late_evidence_and_changes_signature():
    """
    请求在途时停用、换地址或换令牌都丢弃迟到正文，下一调用重新读取配置。
    """
    config = MagicMock()
    for changes in ({"enabled": False}, {"token": "mpk_" + "x" * 43}, {"origin": "https://new.example.invalid"}):
        config.get.return_value = _config()

        async def query(_binding, _request):
            """
            在返回前模拟一次管理员配置变更。

            :param _binding: 请求开始时的完整配置
            :param _request: 当前短查询
            :return: 必须被上层丢弃的合成证据
            """
            config.get.return_value = _config(**changes)
            return KnowledgeOutcome("matched", None)

        port = MagicMock(query=AsyncMock(side_effect=query))
        service = KnowledgeService(config, port, lambda: "3.2.5")
        previous = service.signature()
        assert TOKEN not in previous and ORIGIN not in previous
        assert await service.search("规则") == KnowledgeOutcome("configuration_changed")
        assert service.signature() != previous


@pytest.mark.asyncio
async def test_service_total_deadline_cancels_port_and_invalid_inputs_never_dispatch(monkeypatch):
    """
    用例总预算包括端口的全部等待，超时取消；无效输入不消耗网络请求。
    """
    config = MagicMock()
    config.get.return_value = _config()
    cancelled = asyncio.Event()

    async def query(_binding, _request):
        """
        模拟能响应取消的无限外部等待。

        :param _binding: 本轮配置
        :param _request: 本次协议请求
        :return: 不会正常返回
        """
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    port = MagicMock(query=AsyncMock(side_effect=query))
    service = KnowledgeService(config, port, lambda: None)
    assert await service.search("x" * 301) == KnowledgeOutcome("invalid_request")
    assert await service.search("  ") == KnowledgeOutcome("invalid_request")
    assert await service.read("../path") == KnowledgeOutcome("invalid_request")
    port.query.assert_not_awaited()
    monkeypatch.setattr("app.application.knowledge.QUERY_SECONDS", 0.01)
    assert await service.search("规则") == KnowledgeOutcome("timeout")
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_configuration_change_event_never_broadcasts_center_credentials(monkeypatch):
    """
    系统配置事件只通知启用状态，不把中心地址和令牌送入插件监听器。
    """
    from app.startup.composition import system as composition

    observed = []

    async def capture(**kwargs):
        """
        保存合成事件供断言，不发出真实广播。

        :param kwargs: EventManager 的类型和数据参数
        :return: 已完成的空结果
        """
        observed.append(kwargs)

    monkeypatch.setattr(composition.eventmanager, "async_send_event_strict", capture)
    await composition._ConfigurationEventAdapter().publish(SystemConfigKey.AIAgentKnowledge, _config())
    payload = observed[0]["data"]
    assert payload.value == {"enabled": True}
    assert TOKEN not in repr(payload) and ORIGIN not in repr(payload)
    observed.clear()
    await composition._ConfigurationEventAdapter().publish(
        {SystemConfigKey.AIAgentKnowledge.value: None}.keys(), _config()
    )
    payload = observed[0]["data"]
    assert payload.value is None
    assert TOKEN not in repr(payload) and ORIGIN not in repr(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [TOKEN, REFERENCE, "Cookie: uid=123; pass=secret", "Authorization: Bearer abc",
                                       "https://site.invalid/?passkey=syntheticsecret", "sk-" + "z" * 24])
async def test_known_credential_shapes_are_rejected_before_transport(secret):
    """
    已知凭据形态在实例侧拒绝，不能依赖中心收到后再过滤。
    """
    config, port = MagicMock(), MagicMock(query=AsyncMock())
    config.get.return_value = _config()
    service = KnowledgeService(config, port, lambda: "3.2.5")
    assert await service.search(secret) == KnowledgeOutcome("invalid_request")
    assert await service.search("规则", site=secret) == KnowledgeOutcome("invalid_request")
    port.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_and_read_do_not_reuse_cached_evidence_or_prior_credential():
    """
    连续检索与补读都访问当前端口，令牌轮换后不复用旧授权或正文。
    """
    config, port = MagicMock(), MagicMock(query=AsyncMock(return_value=KnowledgeOutcome("no_match")))
    config.get.return_value = _config()
    service = KnowledgeService(config, port, lambda: "3.2.5")
    await service.search("规则")
    await service.search("规则")
    config.get.return_value = _config(token="mpk_" + "x" * 43)
    await service.read(REFERENCE)
    assert port.query.await_count == 3
    binding, request = port.query.await_args.args
    assert binding.token == "mpk_" + "x" * 43 and request.evidence_id == REFERENCE


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"domain": "pt"}, {"product": "another-product"}])
async def test_search_rejects_response_from_another_declared_scope(change):
    """
    响应的产品与已指定领域必须匹配当前查询，不盲信错误中心返回的范围。
    """
    data = _reply()
    data["evidence"][0].update(change)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _request: httpx2.Response(200, json=data))) as http:
        result = await KnowledgeClient(_requester(http)).query(AgentKnowledgeConfig(**_config()), KnowledgeSearch(query="规则", domain="mp"))
    assert result.status == "invalid_response"
