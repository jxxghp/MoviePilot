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
from app.agent.llm.helper import LLMHelper as CanonicalHelper
from app.helper.llm import LLMHelper as CompatHelper
print(json.dumps({
    "cold": cold,
    "package_identity": PackageHelper is CanonicalHelper,
    "compat_identity": CompatHelper is CanonicalHelper,
    "helper_loaded": "app.agent.llm.helper" in sys.modules,
    "provider_loaded": "app.agent.llm.provider" in sys.modules,
}))
"""
    )

    assert result == {
        "cold": {"helper": False, "provider": False},
        "package_identity": True,
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


def test_llm_package_root_does_not_duplicate_internal_owner_exports() -> None:
    """兼容包根只保留既有 ABI，不得重新导出拆分后的内部 owner。"""
    path = PROJECT_ROOT / "app" / "agent" / "llm" / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in {"_EXPORT_MODULES", "__all__"}
    }
    exported = set(assignments["__all__"]) | set(assignments["_EXPORT_MODULES"])
    forbidden = {
        "LLMProviderRuntimePort",
        "PendingAuthSession",
        "ProviderAuthMethod",
        "ProviderSpec",
        "ProviderUrlPreset",
        "register_llm_provider_runtime",
        "resolve_llm_provider_runtime",
    }

    assert exported.isdisjoint(forbidden)
    assert not any(
        isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("app.agent.llm.")
        for node in tree.body
    )
