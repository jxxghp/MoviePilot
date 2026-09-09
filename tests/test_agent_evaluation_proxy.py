"""模型代理的离线协议、工具隔离、预算、凭据和资源回收回归。"""

import asyncio
import json
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
import pytest

from scripts.evaluation import proxy as proxy_module
from scripts.evaluation.models import ModelSettings
from scripts.evaluation.proxy import EvaluationModelProxy, project_host_instructions, project_tools

SETTINGS = ModelSettings("test-model", "https://model.invalid/v1", "private-test-provider-key", max_model_calls=2)
USAGE = {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}


def _payload(**updates: Any) -> dict[str, Any]:
    """构造与原生 Responses 客户端相同形态的受控工具请求。"""
    return {"model": SETTINGS.model, "tools": [
        {"type": "function", "name": "update_plan", "parameters": {}},
        {"type": "function", "name": "mcp__evaluation__moviepilot_api", "parameters": {}},
        {"type": "custom", "name": "apply_patch", "format": {"type": "text"}},
    ], **updates}


def _completed(**updates: Any) -> dict[str, Any]:
    """提供真实协议的完成信号，用量缺失与返回模型可以独立变化。"""
    return {"id": "response-test", "status": "completed", "model": SETTINGS.model, "usage": USAGE, "output": [], **updates}


def _event(value: Any) -> bytes:
    """生成标准 SSE 帧，测试仍可刻意跨任意字节拆分。"""
    return f"data: {json.dumps(value)}\n\n".encode()


class _ByteStream(httpx.AsyncByteStream):
    """可检查关闭状态的离线字节流，允许模拟帧间故障。"""

    def __init__(self, chunks: list[Any]) -> None:
        """保存测试字节及异常，不持有套接字。"""
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """按固定顺序返回片段，以覆盖真正的增量解析路径。"""
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def aclose(self) -> None:
        """记录代理是否在成功、拒绝和中途失败时回收上游。"""
        self.closed = True


@asynccontextmanager
async def _client(proxy: EvaluationModelProxy) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI 与 MockTransport 两侧都留在内存，不访问真实网络。"""
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(proxy.app), base_url="http://127.0.0.1",
                                    headers={"Authorization": f"Bearer {proxy.bearer_token}"}) as client:
            yield client
    finally:
        await proxy.close()


def test_namespace_projection_preserves_allowed_schema_and_audits_removed_tools() -> None:
    """目录投影保留函数原定义，不能把原生文件能力混入对照世界。"""
    tools = [{"type": "namespace", "name": "functions", "description": "native controls", "tools": _payload()["tools"]}]
    projected, retained, removed = project_tools(tools)
    assert retained == ["functions.update_plan", "functions.mcp__evaluation__moviepilot_api"]
    assert removed == ["functions.apply_patch"]
    assert projected[0]["description"] == "native controls"
    assert projected[0]["tools"] == tools[0]["tools"][:2]
    projected[0]["tools"][0]["parameters"]["changed"] = True
    assert tools[0]["tools"][0]["parameters"] == {}


@pytest.mark.asyncio
async def test_probe_records_catalog_without_forwarding_or_spending_budget() -> None:
    """探测首个请求目录即可继续调试，统计不能把探测伪装为真实模型调用。"""
    outbound = []
    proxy = EvaluationModelProxy(SETTINGS, probe_only=True, transport=httpx.MockTransport(lambda request: outbound.append(request)))
    async with _client(proxy) as client:
        for _ in range(3):
            assert (await client.post("/v1/responses", json=_payload())).status_code == 400
    result = proxy.snapshot()
    assert not outbound and result["model_calls"] == 0 and result["probe_requests"] == 3
    assert result["tokens"] is None and result["blocked_model_calls"] == 0
    result["model_requests"][0]["retained_tools"].append("forged")
    assert "forged" not in proxy.snapshot()["model_requests"][0]["retained_tools"]


@pytest.mark.parametrize("updates", [
    {"tools": None}, {"tools": [None]}, {"tools": [{"type": "function"}]},
    {"tools": [{"type": "function", "name": 42}]}, {"tools": [{"type": "namespace", "name": "functions", "tools": {}}]},
    {"tools": [{"type": "function", "name": "update_plan", "parameters": []}]},
    {"tools": [{"type": "function", "name": "mcp__private__message"}]},
    {"tools": [{"type": "mcp", "server_url": "https://private.invalid"}]},
    {"max_output_tokens": "100"}, {"max_output_tokens": 0}, {"max_output_tokens": -1},
    {"max_output_tokens": True}, {"max_output_tokens": 0.5}, {"max_output_tokens": None},
    {"stream": "true"}, {"model": "different-model"}, {"reasoning": {"effort": "ultra"}},
    {"reasoning": ["high"]}, {"reasoning": []}, {"reasoning": False}, {"tool_choice": {"type": "function", "name": "exec_command"}},
    {"input": [{"type": "custom_tool_call", "name": "apply_patch"}]},
    {"metadata": {SETTINGS.api_key: "hidden-in-key"}},
])
@pytest.mark.asyncio
async def test_invalid_request_is_rejected_before_outbound_or_accounting(updates: dict[str, Any]) -> None:
    """恶意目录、错模型和非法预算必须在真正调用前明确失败。"""
    outbound = []
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda request: outbound.append(request)))
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=_payload(**updates))
    assert response.status_code == 400 and not outbound and proxy.snapshot()["model_calls"] == 0
    assert SETTINGS.api_key not in response.text + json.dumps(proxy.snapshot())


@pytest.mark.asyncio
async def test_request_authentication_origin_and_size_fail_without_forwarding(monkeypatch) -> None:
    """本地地址也需要随机认证，错误来源与过大正文不消耗模型预算。"""
    proxy = EvaluationModelProxy(SETTINGS, probe_only=True)
    monkeypatch.setattr(proxy_module, "MAX_REQUEST_BYTES", 20)
    async with _client(proxy) as client:
        assert (await client.post("/v1/responses", json={}, headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.post("/v1/responses", json={}, headers={"Origin": "https://foreign.invalid"})).status_code == 403
        assert (await client.post("/v1/responses", json=_payload())).status_code == 413
        assert (await client.post("/v1/responses", content="malformed")).status_code == 400
    assert proxy.snapshot()["probe_requests"] == 0


@pytest.mark.asyncio
async def test_forwarding_uses_only_fixed_provider_and_real_key_inside_transport() -> None:
    """真实凭据只进入固定供应商请求，原生客户端看到的令牌不会被转发。"""
    outbound = []

    def handler(request: httpx.Request) -> httpx.Response:
        """捕获私有连接边界并返回完成响应。"""
        outbound.append(request)
        return httpx.Response(200, json=_completed(model="provider-reported-model"))

    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(handler))
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=_payload(max_output_tokens=999999))
    assert response.status_code == 200 and len(outbound) == 1
    request = outbound[0]
    assert str(request.url) == "https://model.invalid/v1/responses"
    assert request.headers["Authorization"] == f"Bearer {SETTINGS.api_key}"
    assert proxy.bearer_token not in str(request.headers)
    payload = json.loads(request.content)
    assert payload["max_output_tokens"] == SETTINGS.max_output_tokens
    assert [tool["name"] for tool in payload["tools"]] == ["update_plan", "mcp__evaluation__moviepilot_api"]
    result = proxy.snapshot()
    assert result["tokens"] == USAGE and result["usage_complete"] is True
    assert result["reported_models"] == ["provider-reported-model"]
    assert SETTINGS.api_key not in response.text + json.dumps(result)


@pytest.mark.asyncio
async def test_parallel_requests_share_atomic_hard_limit_and_unknown_failure_cost() -> None:
    """多子代理同时请求也不能超额，已发送失败请求保留为用量未知。"""
    outbound = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """让请求挂起一个调度点以模拟重叠调用。"""
        outbound.append(request)
        await asyncio.sleep(0)
        return httpx.Response(500, text=SETTINGS.api_key)

    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(handler))
    async with _client(proxy) as client:
        responses = await asyncio.gather(*(client.post("/v1/responses", json=_payload()) for _ in range(5)))
    result = proxy.snapshot()
    assert len(outbound) == result["model_calls"] == 2 and result["blocked_model_calls"] == 3
    assert result["usage_complete"] is False and result["tokens"] is None
    assert all(SETTINGS.api_key not in response.text for response in responses)


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 2}, {**USAGE, "input_tokens": True},
                                          {**USAGE, "input_tokens": -1}, {**USAGE, "input_tokens": 2.5}])
@pytest.mark.asyncio
async def test_partial_or_invalid_usage_stays_unknown(usage: Any) -> None:
    """完成响应缺少完整合法用量时，不能输出虚构的零 token。"""
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_completed(usage=usage))))
    async with _client(proxy) as client:
        assert (await client.post("/v1/responses", json=_payload())).status_code == 200
    result = proxy.snapshot()
    assert result["completed_model_calls"] == 1 and result["tokens"] is None and result["usage_complete"] is False


@pytest.mark.parametrize("output", [
    [{"type": "function_call", "name": "exec_command", "arguments": "{}"}],
    [{"type": "custom_tool_call", "name": "apply_patch", "input": "arbitrary"}],
    [{"type": "computer_call", "action": {"type": "click"}}],
    [{"type": "message", "content": [{"type": "output_text", "text": SETTINGS.api_key}]}],
])
@pytest.mark.asyncio
async def test_non_streaming_forbidden_calls_and_credentials_never_reach_client(output: Any) -> None:
    """非流式同样验证整个完成对象，不能依赖模型遵守已过滤目录。"""
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_completed(output=output))))
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=_payload())
    assert response.status_code == 400 and proxy.snapshot()["completed_model_calls"] == 0
    assert SETTINGS.api_key not in response.text and "exec_command" not in response.text


@pytest.mark.parametrize("ending", [b"\n\n", b"\r\n\r\n", b""])
@pytest.mark.asyncio
async def test_sse_fragmented_frames_and_final_unterminated_event_are_captured(ending: bytes) -> None:
    """帧可以跨 TCP 片段，末尾无空行的完整 JSON 事件也必须处理。"""
    completed = _event({"type": "response.completed", "response": _completed()}).rstrip(b"\n") + ending
    stream = _ByteStream([completed[:3], completed[3:19], completed[19:]])
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=_payload(stream=True))
    assert response.status_code == 200 and "response.completed" in response.text and stream.closed
    assert proxy.snapshot()["tokens"] == USAGE and proxy.snapshot()["completed_model_calls"] == 1


@pytest.mark.parametrize("bad_event", [
    {"type": "response.output_item.added", "item": {"type": "function_call", "name": "exec_command"}},
    {"type": "response.output_item.done", "item": {"type": "custom_tool_call", "name": "apply_patch"}},
    {"type": "response.completed", "response": _completed(output=[{"type": "function_call", "name": "exec_command"}])},
    {"type": "response.output_text.delta", "delta": SETTINGS.api_key},
])
@pytest.mark.asyncio
async def test_sse_blocks_unsafe_frame_before_native_execution(bad_event: dict[str, Any]) -> None:
    """已经转发安全进度不代表后续帧可信，非法调用帧须阻断并标记失败。"""
    safe = _event({"type": "response.created", "response": {"id": "safe-progress"}})
    stream = _ByteStream([safe, _event(bad_event), _event({"type": "response.completed", "response": _completed()})])
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=_payload(stream=True))
    assert "safe-progress" in response.text and "evaluation_error" in response.text and stream.closed
    assert "exec_command" not in response.text and "apply_patch" not in response.text and SETTINGS.api_key not in response.text
    result = proxy.snapshot()
    assert result["completed_model_calls"] == 0 and result["model_requests"][0]["error_type"] == "invalid_stream"


@pytest.mark.asyncio
async def test_credentials_split_across_deltas_cannot_be_reassembled_by_client() -> None:
    """密钥拆成多个合法 JSON 帧时，形成完整秘密的帧仍必须被扣留。"""
    key = SETTINGS.api_key
    frames = [_event({"type": "response.output_text.delta", "item_id": "message-one", "delta": part})
              for part in (key[:7], key[7:15], key[15:])]
    stream = _ByteStream(frames)
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=_payload(stream=True))
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert key not in "".join(event.get("delta", "") for event in events)
    assert stream.closed and proxy.snapshot()["completed_model_calls"] == 0


@pytest.mark.parametrize("chunks", [[b"data: invalid\n\n"], [b"data: {"],
                                     [_event({"type": "response.created"})], [httpx.ReadError("private failure")]])
@pytest.mark.asyncio
async def test_malformed_truncated_or_failed_stream_has_unknown_completion(chunks: list[Any]) -> None:
    """没有完整终态或网络中断的流必须保留失败证据，且关闭上游。"""
    stream = _ByteStream(chunks)
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=_payload(stream=True))
    assert "evaluation_error" in response.text and "private failure" not in response.text and stream.closed
    assert proxy.snapshot()["completed_model_calls"] == 0 and proxy.snapshot()["tokens"] is None


@pytest.mark.asyncio
async def test_oversized_event_and_nonstream_response_are_bounded(monkeypatch) -> None:
    """无分隔符流和大响应都不能无限累计内存。"""
    monkeypatch.setattr(proxy_module, "MAX_EVENT_BYTES", 16)
    monkeypatch.setattr(proxy_module, "MAX_RESPONSE_BYTES", 16)
    for streaming in (False, True):
        stream = _ByteStream([b"data: " + b"x" * 30])
        proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))
        async with _client(proxy) as client:
            response = await client.post("/v1/responses", json=_payload(stream=streaming))
        assert "evaluation_error" in response.text and stream.closed


@pytest.mark.asyncio
async def test_startup_failure_closes_client_and_socket(monkeypatch) -> None:
    """服务启动异常发生在上下文进入前，也必须清理预先分配的资源。"""
    async def fail(_server: Any, **_kwargs: Any) -> None:
        """模拟监听服务子任务在报告就绪前失败。"""
        raise RuntimeError("offline-startup-failure")

    monkeypatch.setattr(proxy_module.uvicorn.Server, "serve", fail)
    proxy = EvaluationModelProxy(SETTINGS)
    with pytest.raises(RuntimeError, match="offline-startup-failure"):
        await proxy.start()
    assert proxy._client.is_closed and proxy._socket.fileno() == -1 and proxy._task.done()
    await proxy.close()
    assert proxy.snapshot()["server_error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_native_host_instructions_are_removed_without_changing_core_or_user_task() -> None:
    """只移除精确原生用户说明块，不能改写原生系统提示或邻接的任务内容。"""
    removed = "# AGENTS.md instructions for /synthetic/private\n\n<INSTRUCTIONS>\nsynthetic host preference\n</INSTRUCTIONS>"
    outbound = []

    def handler(request: httpx.Request) -> httpx.Response:
        """保留供应商请求以断言正文仅含允许的评测上下文。"""
        outbound.append(json.loads(request.content))
        return httpx.Response(200, json=_completed())

    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(handler))
    core = "native core instructions including literal # AGENTS.md instructions example"
    ordinary = {"type": "input_text", "text": "business task must remain identical"}
    nested = {"role": "user", "content": [{"type": "text", "kind": "agents_md.instructions", "text": removed}, ordinary]}
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=_payload(
            instructions=core, input=[{"role": "user", "content": [{"type": "input_text", "text": removed}, ordinary]},
                                      {"nested_history": [nested]}],
        ))
    assert response.status_code == 200 and outbound[0]["instructions"] == core
    assert outbound[0]["input"][0]["content"] == [ordinary]
    assert outbound[0]["input"][1]["nested_history"][0]["content"] == [ordinary]
    records = proxy.snapshot()["model_requests"][0]["removed_host_instructions"]
    assert len(records) == 2 and records[0]["bytes"] == len(removed.encode()) and len(records[0]["sha256"]) == 64
    assert "synthetic host preference" not in json.dumps(proxy.snapshot())


@pytest.mark.parametrize("block", [
    {"type": "input_text", "text": "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nmissing closing tag"},
    {"type": "input_text", "text": "quoted # AGENTS.md instructions should not be silently removed"},
    {"type": "output_text", "text": "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nbody\n</INSTRUCTIONS>"},
    {"type": "text", "kind": "foreign.kind", "text": "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nbody\n</INSTRUCTIONS>"},
    {"type": "input_text", "kind": "agents_md.instructions", "text": "missing known envelope"},
])
@pytest.mark.asyncio
async def test_ambiguous_host_instructions_abort_before_model_request(block: dict[str, Any]) -> None:
    """无法确认的宿主文本必须停止评测，不能任意删改内容或泄漏给供应商。"""
    outbound = []
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda request: outbound.append(request)))
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=_payload(input=[{"role": "user", "content": [block]}]))
    assert response.status_code == 400 and not outbound and proxy.snapshot()["model_calls"] == 0


@pytest.mark.asyncio
async def test_standalone_host_message_is_omitted_in_probe_audit() -> None:
    """完整用户消息只有宿主块时必须连空消息一起去除，避免产生无效 Responses 输入。"""
    text = "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nhost\n</INSTRUCTIONS>"
    payload = _payload(input=[{"role": "user", "content": [{"type": "input_text", "text": text}]},
                              {"role": "user", "content": [{"type": "input_text", "text": "business"}]}])
    projected, audit = project_host_instructions(payload)
    assert len(projected["input"]) == 1 and len(audit) == 1
    proxy = EvaluationModelProxy(SETTINGS, probe_only=True)
    async with _client(proxy) as client:
        assert (await client.post("/v1/responses", json=payload)).status_code == 400
    assert len(proxy.snapshot()["model_requests"][0]["removed_host_instructions"]) == 1


@pytest.mark.parametrize("text", ["<skills_instructions>host skills</skills_instructions>",
                                  "ordinary <INSTRUCTIONS>unrecognized host</INSTRUCTIONS>"])
@pytest.mark.asyncio
async def test_unknown_host_skill_boundaries_abort_with_text_fingerprint_only(text: str) -> None:
    """原生宿主技能残留未识别时拒绝出站，保留可私下比对的指纹证据。"""
    outbound = []
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda request: outbound.append(request)))
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=_payload(input=[{"role": "user", "content": [{"type": "input_text", "text": text}]}]))
    result = proxy.snapshot()
    assert response.status_code == 400 and not outbound
    metadata = result["rejected_requests"][0]["input_blocks"][0]
    assert metadata["role"] == "user" and metadata["type"] == "input_text" and metadata["bytes"] == len(text.encode())
    assert text not in json.dumps(result) and len(metadata["sha256"]) == 64


@pytest.mark.asyncio
async def test_clean_probe_reports_only_projected_input_fingerprints() -> None:
    """探测报告可核对送出任务完全相同，又不会持久化任务和宿主说明正文。"""
    proxy = EvaluationModelProxy(SETTINGS, probe_only=True)
    async with _client(proxy) as client:
        await client.post("/v1/responses", json=_payload(input=[{"role": "user", "content": [{"type": "input_text", "text": "business task"}]}]))
    result = proxy.snapshot()
    assert result["model_requests"][0]["input_blocks"][0]["bytes"] == 13
    fields = result["model_requests"][0]["request_field_types"]
    assert fields["tools"] == {"type": "list", "length": 2}
    assert fields["model"] == {"type": "str", "length": len(SETTINGS.model)}
    assert "business task" not in json.dumps(result)


def _search_tool(description: str = 'Tools from the following sources:\n- evaluation: Controlled world') -> dict[str, Any]:
    """构造已在原生线协议观察到的客户端工具搜索定义。"""
    return {"type": "tool_search", "execution": "client", "description": description,
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}


def _native_payload(**updates: Any) -> dict[str, Any]:
    """原生工具随 additional_tools 输入传递，不应被代理改成空的顶层 tools。"""
    return {"model": SETTINGS.model, "input": [{"type": "additional_tools", "id": "synthetic-catalog", "role": "developer", "tools": [
        {"type": "namespace", "name": "functions", "tools": _payload()["tools"]},
        {"type": "namespace", "name": "clock", "tools": [{"type": "function", "name": "curr_time", "parameters": {}}]},
        {"type": "namespace", "name": "collaboration", "tools": [
            {"type": "function", "name": name, "parameters": {}} for name in
            ("spawn_agent", "followup_task", "interrupt_agent", "list_agents", "send_message", "wait_agent")
        ]}, _search_tool(),
    ]}], **updates}


@pytest.mark.asyncio
async def test_native_additional_tools_are_projected_in_place_and_audited() -> None:
    """真实原生目录位置必须执行同样隔离，不能只检查不存在的顶层字段。"""
    outbound = []

    def handler(request: httpx.Request) -> httpx.Response:
        """捕获投影后的 wire 结构，不连接任何外部服务。"""
        outbound.append(json.loads(request.content))
        return httpx.Response(200, json=_completed())

    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(handler))
    async with _client(proxy) as client:
        assert (await client.post("/v1/responses", json=_native_payload())).status_code == 200
    assert "tools" not in outbound[0]
    catalog = outbound[0]["input"][0]["tools"]
    assert [tool.get("name", tool["type"]) for tool in catalog] == ["functions", "collaboration", "tool_search"]
    record = proxy.snapshot()["model_requests"][0]
    assert record["removed_tools"] == ["functions.apply_patch", "clock.curr_time"]
    assert len(record["retained_tools"]) == 9 and "collaboration.spawn_agent" in record["retained_tools"]
    assert record["tool_catalogs"][0]["location"] == "input[0].tools"


@pytest.mark.parametrize("catalog", [
    [{"type": "namespace", "name": "unknown", "tools": [{"type": "function", "name": "update_plan"}]}],
    [_search_tool('Tools from sources:\n- evaluation: fake\n- private: external')],
    [_search_tool('No explicit source list')],
    [{**_search_tool(), "execution": "server"}],
])
@pytest.mark.asyncio
async def test_native_unknown_namespace_or_foreign_discovery_aborts_locally(catalog: list[dict[str, Any]]) -> None:
    """原生未知目录与搜索源污染必须拒绝，而非静默缩减后宣称隔离成功。"""
    outbound = []
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda request: outbound.append(request)))
    payload = _native_payload()
    payload["input"][0]["tools"] = catalog
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=payload)
    assert response.status_code == 400 and not outbound


@pytest.mark.parametrize("updates", [{"type": "additional_tools_v2"}, {"role": "user"}, {"unexpected": "field"}, {"tools": None}])
@pytest.mark.asyncio
async def test_native_unknown_additional_tools_shape_aborts_locally(updates: dict[str, Any]) -> None:
    """额外工具消息未知变体不能绕过目录投影。"""
    outbound = []
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda request: outbound.append(request)))
    payload = _native_payload()
    payload["input"][0].update(updates)
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=payload)
    assert response.status_code == 400 and not outbound


def _search_call(**updates: Any) -> dict[str, Any]:
    """原生工具搜索调用使用独立响应变体，参数不是 function_call.arguments 字符串。"""
    return {"type": "tool_search_call", "id": "tool-search-one", "call_id": "search-one", "execution": "client",
            "arguments": {"query": "MoviePilot API Skill", "limit": 3}, **updates}


def _search_output(**updates: Any) -> dict[str, Any]:
    """模拟原生搜索注册目录后合并出的 MCP 命名空间结果。"""
    return {"type": "tool_search_output", "call_id": "search-one", "status": "completed", "execution": "client", "tools": [
        {"type": "namespace", "name": "mcp__evaluation", "tools": [
            {"type": "function", "name": name, "parameters": {}} for name in ("moviepilot_api", "read_skill", "read_tool_result")
        ]},
    ], **updates}


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [3, None])
async def test_native_client_discovery_call_and_namespace_output_are_allowed(limit: Optional[int]) -> None:
    """真实搜索调用返回完整命名空间后，下一次模型请求仍只收到同一评测目录。"""
    call = _search_call(arguments={"query": "MoviePilot API Skill", "limit": limit})
    frames = [_event({"type": "response.output_item.added", "item": _search_call(arguments={})}),
              _event({"type": "response.output_item.done", "item": call}),
              _event({"type": "response.completed", "response": _completed(output=[call])})]
    stream = _ByteStream(frames)
    outbound = []

    def handler(request: httpx.Request) -> httpx.Response:
        """第一次返回搜索指令，第二次确认已发现工具的后续请求。"""
        outbound.append(json.loads(request.content))
        return httpx.Response(200, stream=stream) if len(outbound) == 1 else httpx.Response(200, json=_completed())

    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(handler))
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=_native_payload(stream=True))
        assert "tool_search_call" in response.text and "evaluation_error" not in response.text
        payload = _native_payload()
        payload["input"].extend([call, _search_output()])
        assert (await client.post("/v1/responses", json=payload)).status_code == 200
    assert "tools" not in outbound[1]
    record = proxy.snapshot()["model_requests"][1]
    assert record["tool_catalogs"][-1]["location"] == "input[2].tools"
    assert record["tool_catalogs"][-1]["retained_tools"] == [
        "mcp__evaluation.moviepilot_api", "mcp__evaluation.read_skill", "mcp__evaluation.read_tool_result",
    ]


@pytest.mark.parametrize("updates", [
    {"execution": "server"}, {"call_id": None}, {"call_id": ""}, {"arguments": {"query": "  "}},
    {"arguments": {"query": "x", "sources": ["external"]}}, {"arguments": {"query": "x", "limit": 0}},
    {"arguments": {"query": "x", "limit": True}}, {"arguments": {"query": "x", "limit": 1.5}},
    {"arguments": '{"query":"x"}'},
])
@pytest.mark.asyncio
async def test_native_discovery_rejects_server_execution_and_unknown_arguments(updates: dict[str, Any]) -> None:
    """工具搜索只能操作已注册本地目录，不能让模型提供外部来源或服务端执行指令。"""
    stream = _ByteStream([_event({"type": "response.output_item.done", "item": _search_call(**updates)})])
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=_native_payload(stream=True))
    assert "evaluation_error" in response.text and '"type": "tool_search_call"' not in response.text


@pytest.mark.parametrize("updates", [
    {"execution": "server"}, {"call_id": None}, {"status": "unknown"}, {"tools": None},
    {"tools": [{"type": "function", "name": "mcp__private__read_secret", "parameters": {}}]},
    {"tools": [{"type": "custom", "name": "apply_patch"}]},
    {"tools": [{"type": "function", "name": "update_plan", "parameters": {}}]},
])
@pytest.mark.asyncio
async def test_native_discovery_output_cannot_inject_other_tool_definitions(updates: dict[str, Any]) -> None:
    """工具搜索输出也是目录入口，不能通过历史结果或模型返回另行引入工具。"""
    outbound = []
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda request: outbound.append(request)))
    payload = _native_payload()
    payload["input"].append(_search_output(**updates))
    async with _client(proxy) as client:
        response = await client.post("/v1/responses", json=payload)
    assert response.status_code == 400 and not outbound


class _WaitingStream(_ByteStream):
    """发出首帧后等待，复现客户端不等供应商结束连接的真实 SSE 行为。"""

    def __init__(self, first: bytes) -> None:
        """保留尾部读取事实，用于证明终态后代理主动停止读取。"""
        super().__init__([first])
        self.tail_read = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """尾部故意不结束，由客户端断开或代理主动关闭来结束本次响应。"""
        yield self.chunks[0]
        self.tail_read = True
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_terminal_frame_closes_upstream_without_waiting_for_done_or_eof() -> None:
    """原生收到完成事件即结束，代理不能继续等待尾帧并把正常结束误记为取消。"""
    stream = _WaitingStream(_event({"type": "response.completed", "response": _completed()}))
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))
    async with _client(proxy) as client:
        async with asyncio.timeout(1):
            response = await client.post("/v1/responses", json=_payload(stream=True))
    assert "response.completed" in response.text and stream.closed and not stream.tail_read
    result = proxy.snapshot()
    assert result["completed_model_calls"] == 1 and result["tokens"] == USAGE
    assert result["model_requests"][0]["error_type"] is None


@pytest.mark.parametrize("completed", [True, False])
@pytest.mark.parametrize("cancel", [True, False])
@pytest.mark.asyncio
async def test_generator_close_or_cancellation_preserves_only_verified_terminal(completed: bool, cancel: bool) -> None:
    """生成器取消与关闭都区分已观察终态和提前中断，不抹掉已完成模型的真实消耗。"""
    event = {"type": "response.completed", "response": _completed()} if completed else {"type": "response.created"}
    stream = _WaitingStream(_event(event))
    upstream = httpx.Response(200, stream=stream)
    proxy = EvaluationModelProxy(SETTINGS)
    record = {"completed": False, "usage": None, "error_type": None}
    body = proxy._stream(upstream, record)
    await anext(body)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await body.athrow(asyncio.CancelledError())
    else:
        await body.aclose()
    await proxy.close()
    assert stream.closed and record["completed"] is completed
    if completed:
        assert record["usage"] == USAGE and record["error_type"] is None and record["client_closed_after_terminal"] is True
    else:
        assert record["usage"] is None and record["error_type"] == "stream_cancelled"


@pytest.mark.parametrize("completed", [True, False])
@pytest.mark.asyncio
async def test_real_asgi_client_disconnect_keeps_terminal_and_rejects_early_close(completed: bool) -> None:
    """通过真实 ASGI 断开事件取消流式传输，验证客户端结束时的最终审计结果。"""
    event = {"type": "response.completed", "response": _completed()} if completed else {"type": "response.created"}
    stream = _WaitingStream(_event(event))
    proxy = EvaluationModelProxy(SETTINGS, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))
    disconnect = asyncio.Event()
    request_sent = False
    delivered = []

    async def receive() -> dict[str, Any]:
        """首个事件传入请求，客户端收到首帧后立刻报告断开。"""
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": json.dumps(_payload(stream=True)).encode(), "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        """完成交付首帧后让断开监听器抢占，覆盖真正传输生命周期中的取消。"""
        if message["type"] == "http.response.body" and message.get("body"):
            delivered.append(message["body"])
            disconnect.set()
            await asyncio.sleep(0)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "method": "POST",
             "scheme": "http", "path": "/v1/responses", "raw_path": b"/v1/responses", "query_string": b"",
             "root_path": "", "http_version": "1.1", "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 1234),
             "headers": [(b"authorization", f"Bearer {proxy.bearer_token}".encode())]}
    try:
        async with asyncio.timeout(1):
            await proxy.app(scope, receive, send)
        await asyncio.sleep(0)
    finally:
        await proxy.close()
    assert delivered and stream.closed
    result = proxy.snapshot()
    assert result["completed_model_calls"] == int(completed)
    assert result["tokens"] == (USAGE if completed else None)
    assert result["model_requests"][0]["error_type"] == (None if completed else "stream_cancelled")


@pytest.mark.asyncio
async def test_proxy_start_and_close_do_not_replace_host_signal_handlers() -> None:
    """本地模型服务与 MCP 共用控制器时，启动和关闭都不能接管进程全局信号。"""
    signals = (signal.SIGINT, signal.SIGTERM)
    previous = {item: signal.getsignal(item) for item in signals}
    async with EvaluationModelProxy(SETTINGS, probe_only=True):
        assert {item: signal.getsignal(item) for item in signals} == previous
    assert {item: signal.getsignal(item) for item in signals} == previous
