"""Agent 工具与 LLM 入口的延迟加载合同测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch


def _run_isolated(script: str) -> dict:
    """在全新解释器中执行导入探针，避免当前 pytest 模块缓存干扰。"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_mcp_router_import_keeps_agent_tool_runtime_cold() -> None:
    """默认 API 路由加载不得提前物化工具目录或 Agent 编排。"""
    result = _run_isolated(
        """
import json
import sys

import app.api.endpoints.mcp

forbidden = (
    "app.agent.callback",
    "app.agent.orchestrator",
    "app.agent.tools.base",
    "app.agent.tools.catalog",
    "app.agent.tools.factory",
    "app.agent.tools.impl",
    "anthropic",
    "boto3",
    "google.genai",
    "langchain",
    "langgraph",
    "openai",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
print(json.dumps({"loaded": loaded}))
"""
    )

    assert result == {"loaded": []}


def test_agent_initializer_import_only_registers_lazy_providers() -> None:
    """组合根导入只注册 provider，不得提前加载 Agent 重量实现。"""
    result = _run_isolated(
        """
import json
import sys

import app.startup.initializers.agent

forbidden = (
    "app.agent.orchestrator",
    "app.agent.llm.capability",
    "app.agent.llm.helper",
    "app.agent.llm.provider",
    "app.agent.prompt",
    "app.agent.tools.base",
    "app.agent.tools.factory",
    "app.agent.tools.impl",
    "anthropic",
    "langchain",
    "langgraph",
    "openai",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
print(json.dumps({"loaded": loaded}))
"""
    )

    assert result == {"loaded": []}


def test_manager_constructor_and_llm_facade_are_lightweight() -> None:
    """构造全局 manager 与导入 LLM facade 都不加载真实目录或 provider。"""
    result = _run_isolated(
        """
import json
import sys

from app.agent.tools.manager import MoviePilotToolsManager
import app.agent.llm

manager = MoviePilotToolsManager(session_id="lazy", user_id="api")
forbidden = (
    "app.agent.llm.capability",
    "app.agent.llm.helper",
    "app.agent.llm.provider",
    "app.agent.tools.base",
    "app.agent.tools.catalog",
    "app.agent.tools.factory",
    "app.agent.tools.impl",
    "anthropic",
    "boto3",
    "google.genai",
    "langchain",
    "langchain_core",
    "openai",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
print(json.dumps({
    "loaded": loaded,
    "tools": manager.tools,
    "catalog": manager.catalog,
}))
"""
    )

    assert result == {"loaded": [], "tools": [], "catalog": None}


def test_tool_catalog_materialization_does_not_load_streaming_callback() -> None:
    """工具目录和 schema 首用不应加载仅在真实编排中需要的回调实现。"""
    result = _run_isolated(
        """
import json
import sys
from typing import get_args, get_type_hints

from app.testing.bootstrap import ensure_sites_stub

ensure_sites_stub()
from app.agent.runtime_loader import get_tool_factory

factory = get_tool_factory()
catalog = factory.create_catalog(session_id="lazy", user_id="api")
from app.agent.tools.base import MoviePilotTool

hints = get_type_hints(MoviePilotTool.set_stream_handler)
handler_args = get_args(hints["stream_handler"])
print(json.dumps({
    "callback_loaded": "app.agent.callback" in sys.modules,
    "catalog_entries": len(catalog.entries),
    "handler_types": [item.__name__ for item in handler_args],
}))
"""
    )

    assert result["callback_loaded"] is False
    assert result["catalog_entries"] > 0
    assert result["handler_types"] == ["_StreamingHandlerProtocol", "NoneType"]


def test_legacy_streaming_handler_import_keeps_canonical_identity() -> None:
    """历史显式与星号导入必须按需返回真实 callback 类。"""
    result = _run_isolated(
        """
import json
import sys

import app.agent.tools.base as base
cold_before_explicit = "app.agent.callback" not in sys.modules
from app.agent.tools.base import StreamingHandler
from app.agent.callback import StreamingHandler as CanonicalStreamingHandler

namespace = {}
exec("from app.agent.tools.base import *", namespace)
print(json.dumps({
    "cold_before_explicit": cold_before_explicit,
    "explicit_identity": StreamingHandler is CanonicalStreamingHandler,
    "star_identity": namespace["StreamingHandler"] is CanonicalStreamingHandler,
}))
"""
    )

    assert result == {
        "cold_before_explicit": True,
        "explicit_identity": True,
        "star_identity": True,
    }


def test_manager_first_catalog_use_is_single_flight(monkeypatch) -> None:
    """并发首次查询只能在 manager 锁内建立一次会话工具快照。"""
    from app.agent import runtime_loader
    from app.agent.tools.manager import MoviePilotToolsManager

    calls: list[tuple[str, str]] = []
    fake_tool = SimpleNamespace(
        name="demo",
        description="demo tool",
        args_schema=None,
        _require_admin=False,
    )

    class _Factory:
        """记录目录构造次数的轻量工厂替身。"""

        @classmethod
        def create_catalog(cls, **kwargs):
            calls.append((kwargs["session_id"], kwargs["user_id"]))
            time.sleep(0.05)
            return SimpleNamespace(tools=[fake_tool], plugin_revision=0)

    monkeypatch.setattr(runtime_loader, "get_tool_factory", lambda: _Factory)
    manager = MoviePilotToolsManager(session_id="session", user_id="user")
    results: list[list[str]] = []

    def _list_tools() -> None:
        results.append([tool.name for tool in manager.list_tools()])

    threads = [threading.Thread(target=_list_tools) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == [("session", "user")]
    assert results == [["demo"], ["demo"]]
    assert manager.tools == [fake_tool]
    assert manager.catalog is not None


def test_legacy_explicit_tool_refresh_keeps_atomic_catalog_contract(
    monkeypatch,
) -> None:
    """插件显式刷新旧入口应继续发布同一次构造的完整目录快照。"""
    from app.agent import runtime_loader
    from app.agent.tools.manager import MoviePilotToolsManager

    calls: list[int] = []
    fake_tool = SimpleNamespace(name="plugin_tool")
    fake_catalog = SimpleNamespace(
        tools=[fake_tool],
        plugin_revision=9,
    )

    class _Factory:
        """提供固定 revision 快照的轻量工厂替身。"""

        @classmethod
        def create_catalog(cls, **_kwargs):
            calls.append(1)
            return fake_catalog

    monkeypatch.setattr(runtime_loader, "get_tool_factory", lambda: _Factory)
    manager = MoviePilotToolsManager(session_id="session", user_id="user")

    manager._load_tools()

    assert calls == [1]
    assert manager.catalog is fake_catalog
    assert manager.tools == [fake_tool]
    assert manager._plugin_agent_tools_revision == 9


def test_reply_mode_identity_and_display_message_contract() -> None:
    """旧编排路径必须复用同一枚举，展示消息委托保持原有结构。"""
    from app.agent.contracts import ReplyMode, build_display_message
    from app.agent.orchestrator import MoviePilotAgent
    from app.agent.orchestrator import ReplyMode as LegacyReplyMode

    assert LegacyReplyMode is ReplyMode

    contract_message = build_display_message(
        role="assistant",
        content="done",
        attachments=[{"name": "report.txt"}],
        status="streaming",
    )
    legacy_message = MoviePilotAgent.build_display_message(
        role="assistant",
        content="done",
        attachments=[{"name": "report.txt"}],
        status="streaming",
    )

    for message in (contract_message, legacy_message):
        assert message["id"].startswith("assistant-")
        assert isinstance(message["createdAt"], int)
        message.pop("id")
        message.pop("createdAt")
    assert legacy_message == contract_message


def test_llm_facade_resolves_only_requested_public_module() -> None:
    """访问 capability 导出时不应顺带加载 helper 或 provider registry。"""
    result = _run_isolated(
        """
import json
import sys

import app.agent.llm as llm
capability = llm.AgentCapabilityManager
print(json.dumps({
    "module": capability.__module__,
    "helper_loaded": "app.agent.llm.helper" in sys.modules,
    "provider_loaded": "app.agent.llm.provider" in sys.modules,
}))
"""
    )

    assert result == {
        "module": "app.agent.llm.capability",
        "helper_loaded": False,
        "provider_loaded": False,
    }
