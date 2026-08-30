"""Agent API 路由与禁用响应的延迟加载合同。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_isolated(script: str, config_dir: Path) -> dict:
    """在隔离解释器中执行路由探针，并返回末行 JSON 结果。"""
    env = os.environ.copy()
    env.update(
        {
            "AI_AGENT_ENABLE": "false",
            "API_TOKEN": "test-agent-api-token-1234",
            "CONFIG_DIR": str(config_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_full_api_openapi_keeps_agent_runtime_cold(tmp_path: Path) -> None:
    """完整路由与 OpenAPI 注册不得物化 Agent、工具或模型运行时。"""
    result = _run_isolated(
        r"""
import json
import socket
import sys
import types

network_attempts = []

def block_network(*args, **kwargs):
    network_attempts.append(repr(args[:2]))
    raise AssertionError("router import attempted network access")

socket.create_connection = block_network
socket.getaddrinfo = block_network
socket.socket.connect = block_network

sites = types.ModuleType("app.application.site.sites")
sites.SitesHelper = type("SitesHelper", (), {})
sites.__file__ = "<test-stub>"
sys.modules["app.application.site.sites"] = sites

from fastapi import FastAPI
from app.startup.initializers.routers import init_routers

app = FastAPI()
init_routers(app)
paths = set(app.openapi()["paths"])
required_paths = {
    "/api/v1/message/agent/stream",
    "/api/v1/message/agent/sessions",
    "/api/v1/openai/v1/chat/completions",
    "/api/v1/openai/v1/responses",
    "/api/v1/anthropic/v1/messages",
    "/api/v1/llm/manage",
    "/api/v1/mcp",
    "/api/v1/mcp/tools",
}
forbidden = (
    "app.agent.callback",
    "app.agent.llm.helper",
    "app.agent.orchestrator",
    "app.agent.tools.base",
    "app.agent.tools.catalog",
    "app.agent.tools.factory",
    "app.agent.tools.impl",
    "langgraph",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
print(json.dumps({
    "loaded": loaded,
    "missing_paths": sorted(required_paths - paths),
    "network_attempts": network_attempts,
}))
""",
        tmp_path / "router-import",
    )

    assert result == {
        "loaded": [],
        "missing_paths": [],
        "network_attempts": [],
    }


def test_disabled_protocol_requests_preserve_503_without_runtime_load(
    tmp_path: Path,
) -> None:
    """禁用态兼容协议保持 503，并且不会因构造响应加载 Agent。"""
    result = _run_isolated(
        r"""
import asyncio
import json
import socket
import sys
import types
from types import SimpleNamespace

network_attempts = []

def block_network(*args, **kwargs):
    network_attempts.append(repr(args[:2]))
    raise AssertionError("disabled request attempted network access")

socket.create_connection = block_network
socket.getaddrinfo = block_network
socket.socket.connect = block_network

sites = types.ModuleType("app.application.site.sites")
sites.SitesHelper = type("SitesHelper", (), {})
sites.__file__ = "<test-stub>"
sys.modules["app.application.site.sites"] = sites

from fastapi.security import HTTPAuthorizationCredentials
from app import schemas
from app.api.endpoints import anthropic, openai
from app.api.endpoints.anthropic import messages as anthropic_messages
from app.api.endpoints.openai import chat_completions, responses
from app.application.configuration import ApiRuntimeConfig
from app.runtime.config import settings

runtime_config = ApiRuntimeConfig(
    60, False, settings.AI_AGENT_ENABLE,
    api_token=settings.API_TOKEN,
)
anthropic.get_api_runtime_config_snapshot = lambda: runtime_config
openai.get_api_runtime_config_snapshot = lambda: runtime_config

credentials = HTTPAuthorizationCredentials(
    scheme="Bearer",
    credentials=settings.API_TOKEN,
)
request = SimpleNamespace(headers={})

async def run_requests():
    chat_response = await chat_completions(
        payload=schemas.OpenAIChatCompletionsRequest(
            messages=[schemas.OpenAIChatMessage(role="user", content="hello")]
        ),
        request=request,
        credentials=credentials,
    )
    responses_response = await responses(
        payload=schemas.OpenAIResponsesRequest(input="hello"),
        credentials=credentials,
    )
    anthropic_response = await anthropic_messages(
        payload=schemas.AnthropicMessagesRequest(
            messages=[schemas.AnthropicMessage(role="user", content="hello")]
        ),
        x_api_key=settings.API_TOKEN,
    )
    return chat_response, responses_response, anthropic_response

protocol_responses = asyncio.run(run_requests())
forbidden = (
    "app.agent.callback",
    "app.agent.llm.helper",
    "app.agent.orchestrator",
    "app.agent.tools.base",
    "app.agent.tools.catalog",
    "app.agent.tools.factory",
    "app.agent.tools.impl",
    "langgraph",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
print(json.dumps({
    "loaded": loaded,
    "network_attempts": network_attempts,
    "status_codes": [response.status_code for response in protocol_responses],
    "bodies": [json.loads(response.body) for response in protocol_responses],
}, ensure_ascii=False))
""",
        tmp_path / "disabled-requests",
    )

    assert result["loaded"] == []
    assert result["network_attempts"] == []
    assert result["status_codes"] == [503, 503, 503]
    assert result["bodies"][0]["error"]["code"] == "ai_agent_disabled"
    assert result["bodies"][1]["error"]["code"] == "ai_agent_disabled"
    assert result["bodies"][2]["error"]["type"] == "api_error"


def test_runtime_agent_type_factories_are_single_flight(tmp_path: Path) -> None:
    """并发首次解析必须返回同一 class，避免会话复用误判构造器已变化。"""
    result = _run_isolated(
        r"""
import json
import sys
import threading
import time
import types

sites = types.ModuleType("app.application.site.sites")
sites.SitesHelper = type("SitesHelper", (), {})
sites.__file__ = "<test-stub>"
sys.modules["app.application.site.sites"] = sites

from app.agent import web
from app.api.endpoints import openai

def exercise(module, factory_name, getter_name):
    calls = []
    call_lock = threading.Lock()
    start = threading.Barrier(8)

    class RuntimeAgent:
        pass

    def get_runtime_type():
        with call_lock:
            calls.append(1)
        time.sleep(0.02)
        return RuntimeAgent

    setattr(module, getter_name, get_runtime_type)
    factory = getattr(module, factory_name)
    results = []

    def resolve():
        start.wait()
        results.append(factory())

    threads = [threading.Thread(target=resolve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return len(calls), all(result is results[0] for result in results)

web_calls, web_identity = exercise(
    web,
    "_get_web_agent_type",
    "get_moviepilot_agent_type",
)
collecting_calls, collecting_identity = exercise(
    openai,
    "_get_collecting_agent_type",
    "get_moviepilot_agent_type",
)
print(json.dumps({
    "web_calls": web_calls,
    "web_identity": web_identity,
    "collecting_calls": collecting_calls,
    "collecting_identity": collecting_identity,
}))
""",
        tmp_path / "agent-type-single-flight",
    )

    assert result == {
        "web_calls": 1,
        "web_identity": True,
        "collecting_calls": 1,
        "collecting_identity": True,
    }


def test_persistent_protocol_agent_rebinds_stream_queue_without_stale_output(
    tmp_path: Path,
) -> None:
    """稳定协议会话复用 Agent 时必须保留 handler identity 并切换请求队列。"""
    result = _run_isolated(
        r"""
import asyncio
import json
import sys
import types

sites = types.ModuleType("app.application.site.sites")
sites.SitesHelper = type("SitesHelper", (), {})
sites.__file__ = "<test-stub>"
sys.modules["app.application.site.sites"] = sites

from app.api.endpoints import openai

class RuntimeAgent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.stream_handler = object()
        self._compiled_agent_bundle = object()

class TestStreamingHandler(openai._OpenAIStreamingHandlerMixin):
    pass

openai._get_openai_streaming_handler_type = lambda: TestStreamingHandler
agent_type = openai._build_collecting_agent_type(RuntimeAgent)
agent = agent_type(session_id="stable", user_id="api")
first_queue = asyncio.Queue()
second_queue = asyncio.Queue()

agent.configure_protocol_request(stream_mode=True, event_queue=first_queue)
handler = agent.stream_handler
handler._event_queue.put_nowait("first")
compiled_bundle = object()
agent._compiled_agent_bundle = compiled_bundle

agent.configure_protocol_request(stream_mode=True, event_queue=second_queue)
agent.release_protocol_request(first_queue)
handler._event_queue.put_nowait("second")
agent.release_protocol_request(second_queue)

print(json.dumps({
    "same_handler": agent.stream_handler is handler,
    "same_bundle": agent._compiled_agent_bundle is compiled_bundle,
    "first": first_queue.get_nowait(),
    "first_empty": first_queue.empty(),
    "second": second_queue.get_nowait(),
    "second_empty": second_queue.empty(),
    "released": handler._event_queue is None,
}))
""",
        tmp_path / "protocol-stream-rebind",
    )

    assert result == {
        "same_handler": True,
        "same_bundle": True,
        "first": "first",
        "first_empty": True,
        "second": "second",
        "second_empty": True,
        "released": True,
    }


def test_protocol_routes_follow_agent_service_lifecycle(tmp_path: Path) -> None:
    """服务未运行时返回 503，运行态仍执行原有兼容协议响应流程。"""
    result = _run_isolated(
        r"""
import asyncio
import json
import socket
import sys
import types
from types import SimpleNamespace

network_attempts = []

def block_network(*args, **kwargs):
    network_attempts.append(repr(args[:2]))
    raise AssertionError("protocol lifecycle test attempted network access")

socket.create_connection = block_network
socket.getaddrinfo = block_network
socket.socket.connect = block_network

sites = types.ModuleType("app.application.site.sites")
sites.SitesHelper = type("SitesHelper", (), {})
sites.__file__ = "<test-stub>"
sys.modules["app.application.site.sites"] = sites

from fastapi.security import HTTPAuthorizationCredentials
from app import schemas
from app.api.endpoints import anthropic, openai
from app.application.configuration import ApiRuntimeConfig
from app.runtime.config import settings

settings.AI_AGENT_ENABLE = True
runtime_config = ApiRuntimeConfig(
    60, False, True,
    api_token=settings.API_TOKEN,
)
anthropic.get_api_runtime_config_snapshot = lambda: runtime_config
openai.get_api_runtime_config_snapshot = lambda: runtime_config
credentials = HTTPAuthorizationCredentials(
    scheme="Bearer",
    credentials=settings.API_TOKEN,
)
request = SimpleNamespace(headers={})
chat_payload = schemas.OpenAIChatCompletionsRequest(
    messages=[schemas.OpenAIChatMessage(role="user", content="hello")]
)
anthropic_payload = schemas.AnthropicMessagesRequest(
    messages=[schemas.AnthropicMessage(role="user", content="hello")]
)

async def run_unavailable():
    return (
        await openai.chat_completions(chat_payload, request, credentials),
        await anthropic.messages(
            anthropic_payload,
            x_api_key=settings.API_TOKEN,
        ),
    )

unavailable = asyncio.run(run_unavailable())

class RuntimeAgent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.stream_handler = object()
        self._compiled_agent_bundle = None

    async def process(self, _prompt, **_kwargs):
        return "runtime reply"

class RunningManager:
    async def process_message(self, **kwargs):
        agent = kwargs["agent_factory"](
            session_id=kwargs["session_id"],
            user_id=kwargs["user_id"],
            channel=kwargs["channel"],
            source=kwargs["source"],
            username=kwargs["username"],
        )
        kwargs["agent_setup"](agent)
        return await agent.process(
            kwargs["message"],
            images=kwargs["images"],
            files=kwargs["files"],
        )

    async def clear_session(self, **_kwargs):
        return None

running_manager = RunningManager()
openai.get_running_agent_manager = lambda: running_manager
anthropic.get_running_agent_manager = lambda: running_manager
openai.get_moviepilot_agent_type = lambda: RuntimeAgent

async def run_available():
    return (
        await openai.chat_completions(chat_payload, request, credentials),
        await anthropic.messages(
            anthropic_payload,
            x_api_key=settings.API_TOKEN,
        ),
    )

available = asyncio.run(run_available())
openai_body = json.loads(available[0].body)
print(json.dumps({
    "unavailable_status": [response.status_code for response in unavailable],
    "unavailable_codes": [
        json.loads(unavailable[0].body)["error"]["code"],
        json.loads(unavailable[1].body)["error"]["type"],
    ],
    "available_openai": openai_body["choices"][0]["message"]["content"],
    "available_anthropic": available[1].content[0].text,
    "network_attempts": network_attempts,
}, ensure_ascii=False))
""",
        tmp_path / "protocol-service-lifecycle",
    )

    assert result == {
        "unavailable_status": [503, 503],
        "unavailable_codes": ["ai_agent_unavailable", "api_error"],
        "available_openai": "runtime reply",
        "available_anthropic": "runtime reply",
        "network_attempts": [],
    }
