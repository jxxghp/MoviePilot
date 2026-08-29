"""LLM 公开 Facade、Gateway 与旧插件导入路径的治理门禁。"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
FACADE_FIXTURE = (
    PROJECT_ROOT / "tests" / "fixtures" / "architecture" / "llm-provider-facade.json"
)


def _run_isolated(script: str) -> dict[str, object]:
    """在全新解释器中运行导入探针，避免当前测试进程模块缓存干扰。"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _manager_facade_snapshot() -> dict[str, object]:
    """从源码提取 Manager 稳定公开方法，不导入重量 provider 实现。"""
    path = PROJECT_ROOT / "app" / "agent" / "llm" / "provider.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    manager = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LLMProviderManager"
    )
    methods = {
        node.name: {
            "async": isinstance(node, ast.AsyncFunctionDef),
            "parameters": ast.unparse(node.args),
            "returns": ast.unparse(node.returns) if node.returns else None,
        }
        for node in manager.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    return {
        "schema_version": 1,
        "module": "app.agent.llm.provider",
        "class": "LLMProviderManager",
        "methods": dict(sorted(methods.items())),
    }


def test_llm_provider_manager_public_facade_matches_signature_snapshot() -> None:
    """Manager 拆分不得静默删除、改名或改变既有公开调用签名。"""
    expected = json.loads(FACADE_FIXTURE.read_text(encoding="utf-8"))

    assert _manager_facade_snapshot() == expected


def test_llm_helper_three_paths_share_identity_and_keep_provider_lazy() -> None:
    """包根、canonical 子模块与旧 helper 路径必须解析为同一惰性 Helper。"""
    result = _run_isolated(
        """
import json
import sys

import app.agent.llm as facade
cold = {
    "helper": "app.agent.llm.helper" in sys.modules,
    "provider": "app.agent.llm.provider" in sys.modules,
}
from app.agent.llm import LLMHelper as PackageHelper
import app.agent.llm.helper as CanonicalModule
from app.agent.llm.helper import LLMHelper as CanonicalHelper
import app.helper.llm as CompatModule
from app.helper.llm import LLMHelper as CompatHelper
print(json.dumps({
    "cold": cold,
    "package_identity": PackageHelper is CanonicalHelper,
    "compat_module_identity": CompatModule is CanonicalModule,
    "compat_identity": CompatHelper is CanonicalHelper,
    "helper_loaded": "app.agent.llm.helper" in sys.modules,
    "provider_loaded": "app.agent.llm.provider" in sys.modules,
}))
"""
    )

    assert result == {
        "cold": {"helper": False, "provider": False},
        "package_identity": True,
        "compat_module_identity": True,
        "compat_identity": True,
        "helper_loaded": True,
        "provider_loaded": False,
    }


def test_llm_gateway_protocol_and_registration_delegate_to_one_runtime() -> None:
    """Gateway 只保存组合根 runtime，并完整声明 Helper/API 所需 Facade 协议。"""
    from app.agent.llm import gateway

    protocol_methods = {
        name
        for name, value in vars(gateway.LLMProviderRuntimePort).items()
        if callable(value) and not name.startswith("_")
    }
    assert protocol_methods == {
        "create_bedrock_client",
        "handle_chatgpt_callback",
        "list_models",
        "provider_manage",
        "resolve_cached_model_metadata",
        "resolve_model_list_base_url",
        "resolve_runtime",
    }

    runtime = object()
    previous = gateway.register_llm_provider_runtime(lambda: runtime)
    try:
        assert gateway.resolve_llm_provider_runtime() is runtime
    finally:
        gateway.register_llm_provider_runtime(previous)


def test_agent_package_root_uses_only_precise_compat_symbols() -> None:
    """Agent 包根保持冷启动，旧公开符号精确解析且未知编排内部不再泄漏。"""
    result = _run_isolated(
        """
import json
import sys

import app.agent as facade
cold = "app.agent.orchestrator" not in sys.modules
from app.agent import AgentChain, AgentManager, MoviePilotAgent, ReplyMode, agent_manager
from app.agent.contracts import ReplyMode as CanonicalReplyMode
from app.agent.orchestrator import (
    AgentManager as CanonicalManager,
    MoviePilotAgent as CanonicalAgent,
    agent_manager as canonical_manager,
)
from app.chain.agent import AgentChain as CanonicalChain
try:
    facade.AgentManagerQueueFullError
except AttributeError:
    unknown_blocked = True
else:
    unknown_blocked = False
print(json.dumps({
    "cold": cold,
    "chain_identity": AgentChain is CanonicalChain,
    "manager_identity": AgentManager is CanonicalManager,
    "agent_identity": MoviePilotAgent is CanonicalAgent,
    "reply_mode_identity": ReplyMode is CanonicalReplyMode,
    "singleton_identity": agent_manager is canonical_manager,
    "unknown_blocked": unknown_blocked,
}))
"""
    )

    assert result == {
        "cold": True,
        "chain_identity": True,
        "manager_identity": True,
        "agent_identity": True,
        "reply_mode_identity": True,
        "singleton_identity": True,
        "unknown_blocked": True,
    }


def test_oauth_result_html_escapes_external_error_description() -> None:
    """OAuth provider 返回的错误描述必须作为文本展示，不能注入落地页。"""
    from app.agent.llm.auth import render_auth_result_html

    html = render_auth_result_html(False, '<script>alert("x")</script>')

    assert '<script>alert("x")</script>' not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html


def test_llm_package_root_does_not_duplicate_internal_owner_exports() -> None:
    """LLM 包根不得保留实现或重复导出，旧 Helper 只由 Compat 精确承接。"""
    from app.runtime.compat.manifest import MODULE_ALIASES, SYMBOL_ALIASES

    path = PROJECT_ROOT / "app" / "agent" / "llm" / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    assert all(isinstance(node, ast.Expr) for node in tree.body)
    assert set(SYMBOL_ALIASES["app.agent.llm"]) == {"LLMHelper"}
    helper_alias = MODULE_ALIASES["app.helper.llm"]
    assert helper_alias.target == "app.agent.llm.helper"
    assert helper_alias.replacement == "app.agent.llm.helper"
    assert not helper_alias.is_package


def test_agent_tool_plugin_contracts_keep_canonical_identity() -> None:
    """官方插件使用的工具基类、管理器实例和显式刷新入口保持原 owner。"""
    from app.agent.tools.base import MoviePilotTool
    from app.agent.tools.manager import MoviePilotToolsManager, moviepilot_tool_manager

    assert MoviePilotTool.__module__ == "app.agent.tools.base"
    assert isinstance(moviepilot_tool_manager, MoviePilotToolsManager)
    assert callable(moviepilot_tool_manager._load_tools)
