"""
使用真实宿主 HTTP 适配链和合成传输验证知识协议、默认关闭及配置变化。
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest
from pydantic import ValidationError

from app.adapters.external.knowledge import (
    MAX_RESPONSE_BYTES,
    QUERY_ATTEMPT_SECONDS,
    QUERY_MAX_RETRIES,
    KnowledgeClient,
)
from app.adapters.network.http import AsyncRequestUtils
from app.application.knowledge import KnowledgeService
from app.runtime.config import Settings
from app.schemas.knowledge import AgentKnowledgeConfig, KnowledgeOutcome, KnowledgeRead, KnowledgeSearch

ORIGIN = "https://knowledge.example.invalid"
REFERENCE = "ke_" + "r" * 43
SENTINEL = "本轮合成知识正文，不得输出到默认对象展示"


def _config(**changes):
    """
    构造不对应任何真实中心或用户的完整配置。

    :param changes: 本用例需要替换的配置字段
    :return: 可供服务快照读取的合成字典
    """
    return {"enabled": True, "origin": ORIGIN, **changes}


def _reply(status="matched"):
    """
    对照当前中心实际协议构造完整证据，资料全部为合成值。

    :param status: 需要验证的中心结果状态
    :return: JSON 可序列化的中心响应
    """
    item = {
        "document_id": "a" * 32, "revision": 2, "title": "合成配置规则", "excerpt": SENTINEL,
        "source_label": "合成来源", "collection_label": "合成集合", "source_url": None,
        "source_commit": None, "domain": "pt", "product": "moviepilot", "site_id": None,
        "site_name": "合成示例站",
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


def test_runtime_setting_declares_lab_default_enabled():
    """
    运行时设置目录默认允许 Agent 注入知识工具。
    """
    assert Settings.model_fields["AI_AGENT_KNOWLEDGE_ENABLE"].default is True


@pytest.mark.asyncio
async def test_runtime_toggle_is_read_each_time_and_disables_service(monkeypatch):
    """
    实验室开关变化立即改变工具目录签名，关闭后不请求 Server。
    """
    from app.startup.composition import agent as composition

    state = {"AI_AGENT_KNOWLEDGE_ENABLE": True, "MP_SERVER_HOST": ORIGIN}
    monkeypatch.setattr(composition, "get_runtime_setting", lambda key, default=None: state.get(key, default))
    port = MagicMock(query=AsyncMock(return_value=KnowledgeOutcome("no_match")))
    service = KnowledgeService(composition._agent_knowledge_config, port, lambda: "v3.2.5")
    enabled_signature = service.signature()
    assert service.current_config() is not None
    state["AI_AGENT_KNOWLEDGE_ENABLE"] = False
    assert service.signature() == "disabled"
    assert await service.search("合成规则") == KnowledgeOutcome("disabled")
    port.query.assert_not_awaited()
    state["AI_AGENT_KNOWLEDGE_ENABLE"] = True
    state["MP_SERVER_HOST"] = ""
    assert service.current_config() is None
    assert service.signature() != enabled_signature


@pytest.mark.parametrize("origin", [
    "http://knowledge.example.invalid", "https://user:password@knowledge.example.invalid",
    ORIGIN + "/path", ORIGIN + "?token=x", ORIGIN + "#fragment", ORIGIN + "?",
    ORIGIN + ":bad", ORIGIN + "\n", "https://knowledge.example.invalid\\@other.invalid",
])
def test_config_rejects_ambiguous_server_targets(origin):
    """
    Server 地址不能包含路径、凭据、查询或含糊的解析形式。
    """
    with pytest.raises(ValidationError) as error:
        AgentKnowledgeConfig.model_validate(_config(origin=origin))
    assert ORIGIN not in str(error.value)


@pytest.mark.parametrize("changes", [{"token": "not-accepted"}, {"enabled": "true"}, {"extra": "x"}])
def test_config_defaults_on_and_has_no_per_instance_credential(changes):
    """
    默认打开工具开关，但配置结构不接受用户填写服务端地址以外的访问凭据。
    """
    assert AgentKnowledgeConfig().enabled is True
    assert AgentKnowledgeConfig(origin=ORIGIN + "/").origin == ORIGIN
    with pytest.raises(ValidationError) as error:
        AgentKnowledgeConfig.model_validate(_config(**changes))
    assert "not-accepted" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "read"])
async def test_real_transport_uses_server_path_without_user_authorization(operation):
    """
    真实 AsyncRequestUtils 只向固定 HTTPS 路径发一个 POST，不携带用户令牌。
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
    assert str(request.url) == ORIGIN + "/knowledge/v1/agent/" + operation
    assert request.method == "POST" and "authorization" not in request.headers
    assert "cookie" not in request.headers and "x-api-key" not in request.headers
    assert "x-moviepilot-user-uid" not in request.headers
    assert request.headers["accept-encoding"] == "identity"
    body = json.loads(request.content)
    assert "token" not in body and "origin" not in body and body["protocol_version"] == 1


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
    data = json.loads(json.dumps(_reply()))
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
async def test_transport_failures_are_sanitized_and_retried_within_a_bound(kind):
    """
    只读请求在传输异常后有限重试，最终失败不回显可能含凭据的异常文本。
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
        raise error(SENTINEL, request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        result = await KnowledgeClient(_requester(http)).query(AgentKnowledgeConfig(**_config()), KnowledgeSearch(query="规则"))
    assert result == KnowledgeOutcome("unavailable")
    assert len(calls) == QUERY_MAX_RETRIES + 1 and SENTINEL not in repr(result)


@pytest.mark.asyncio
async def test_transient_transport_failure_retries_and_success_uses_fresh_connection(monkeypatch):
    """
    短暂连接故障后重试成功，并关闭连接复用以便新请求重新解析 DNS。
    """
    calls = []
    options = []

    class SyntheticTransport:
        """
        模拟一次连接失败后返回有效中心响应的宿主请求器。
        """

        async def post_res(self, *_args, **_kwargs):
            """
            首次抛出连接错误，第二次返回合成协议响应。

            :param _args: 请求端口的未使用位置参数
            :param _kwargs: 请求端口的未使用关键字参数
            :return: 合成的成功 HTTP 响应
            """
            calls.append(None)
            if len(calls) == 1:
                raise httpx2.ConnectError(SENTINEL)
            return httpx2.Response(200, json=_reply())

    def create_transport(**kwargs):
        """
        捕获生产请求器选项并返回离线合成传输。

        :param kwargs: 请求器生产配置
        :return: 用于合成响应的传输
        """
        options.append(kwargs)
        return SyntheticTransport()

    monkeypatch.setattr("app.adapters.external.knowledge.AsyncRequestUtils", create_transport)
    result = await KnowledgeClient().query(
        AgentKnowledgeConfig(**_config()), KnowledgeSearch(query="规则"),
    )
    assert result.status == "matched"
    assert len(calls) == 2
    assert len(options) == 2
    assert all(option["timeout"] == QUERY_ATTEMPT_SECONDS for option in options)
    assert all(option["max_keepalive_connections"] == 0 for option in options)


@pytest.mark.asyncio
async def test_service_accepts_slow_response_within_total_budget():
    """
    旧 3 秒上限以外但仍在中心总预算内的有效响应不会被误判超时。
    """

    async def query(_config, _request):
        """
        模拟较慢但在服务总预算内成功的中心响应。

        :param _config: 本轮合成中心配置
        :param _request: 已校验的查询
        :return: 合成的未命中状态
        """
        await asyncio.sleep(3.2)
        return KnowledgeOutcome("no_match")

    port = MagicMock(query=AsyncMock(side_effect=query))
    service = KnowledgeService(lambda: AgentKnowledgeConfig(**_config()), port, lambda: "3.2.9")
    assert await service.search("慢响应测试") == KnowledgeOutcome("no_match")


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
@pytest.mark.parametrize("value", [None, {}, {"enabled": False, "origin": ORIGIN}, _config(origin="")])
async def test_unconfigured_or_disabled_service_never_calls_transport(value):
    """
    缺少 Server 地址或关闭本地开关时服务保持关闭。
    """
    port = MagicMock(query=AsyncMock())
    service = KnowledgeService(lambda: AgentKnowledgeConfig.model_validate(value or {}), port, lambda: "v3.2.5")
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
    port = MagicMock(query=AsyncMock(return_value=KnowledgeOutcome("no_match")))
    service = KnowledgeService(lambda: AgentKnowledgeConfig(**_config()), port, lambda: host_version)
    result = await service.search("  合成规则  ", domain="pt", site="合成站", purpose="troubleshooting")
    assert result.status == "no_match"
    binding, request = port.query.await_args.args
    assert binding.origin == ORIGIN and not hasattr(binding, "token")
    assert request.query == "合成规则" and request.product == "moviepilot"
    assert request.product_version == wire_version and request.limit == 3 and request.site == "合成站"
    assert request.domain == "pt" and request.purpose == "troubleshooting"


@pytest.mark.asyncio
async def test_revocation_during_request_drops_late_evidence_and_changes_signature():
    """
    请求在途时停用、换地址或换令牌都丢弃迟到正文，下一调用重新读取配置。
    """
    state = _config()
    for changes in ({"enabled": False}, {"origin": "https://new.example.invalid"}):
        state.update(_config())

        async def query(_binding, _request):
            """
            在返回前模拟一次管理员配置变更。

            :param _binding: 请求开始时的完整配置
            :param _request: 当前短查询
            :return: 必须被上层丢弃的合成证据
            """
            state.update(changes)
            return KnowledgeOutcome("matched", None)

        port = MagicMock(query=AsyncMock(side_effect=query))
        service = KnowledgeService(lambda: AgentKnowledgeConfig(**state), port, lambda: "3.2.5")
        previous = service.signature()
        assert ORIGIN not in previous
        assert await service.search("规则") == KnowledgeOutcome("configuration_changed")
        assert service.signature() != previous


@pytest.mark.asyncio
async def test_service_total_deadline_cancels_port_and_invalid_inputs_never_dispatch(monkeypatch):
    """
    用例总预算包括端口的全部等待，超时取消；无效输入不消耗网络请求。
    """
    state = _config()
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
    service = KnowledgeService(lambda: AgentKnowledgeConfig(**state), port, lambda: None)
    assert await service.search("x" * 301) == KnowledgeOutcome("invalid_request")
    assert await service.search("  ") == KnowledgeOutcome("invalid_request")
    assert await service.read("../path") == KnowledgeOutcome("invalid_request")
    port.query.assert_not_awaited()
    monkeypatch.setattr("app.application.knowledge.QUERY_SECONDS", 0.01)
    assert await service.search("规则") == KnowledgeOutcome("timeout")
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", ["mpk_" + "s" * 43, REFERENCE, "Cookie: uid=123; pass=secret", "Authorization: Bearer abc",
                                       "https://site.invalid/?passkey=syntheticsecret", "sk-" + "z" * 24])
async def test_known_credential_shapes_are_rejected_before_transport(secret):
    """
    已知凭据形态在实例侧拒绝，不能依赖中心收到后再过滤。
    """
    port = MagicMock(query=AsyncMock())
    service = KnowledgeService(lambda: AgentKnowledgeConfig(**_config()), port, lambda: "3.2.5")
    assert await service.search(secret) == KnowledgeOutcome("invalid_request")
    assert await service.search("规则", site=secret) == KnowledgeOutcome("invalid_request")
    port.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_and_read_do_not_reuse_cached_evidence_or_old_server_address():
    """
    连续检索与补读都访问当前端口，Server 地址变化后不复用旧配置或正文。
    """
    state = _config()
    port = MagicMock(query=AsyncMock(return_value=KnowledgeOutcome("no_match")))
    service = KnowledgeService(lambda: AgentKnowledgeConfig(**state), port, lambda: "3.2.5")
    await service.search("规则")
    await service.search("规则")
    state["origin"] = "https://new.example.invalid"
    await service.read(REFERENCE)
    assert port.query.await_count == 3
    binding, request = port.query.await_args.args
    assert binding.origin == state["origin"] and request.evidence_id == REFERENCE


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
