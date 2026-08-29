"""LLM Provider owner 收敛与 OAuth 输出安全门禁。"""

import ast
from pathlib import Path

from app.agent.llm.auth import render_auth_result_html
from app.agent.llm.provider import LLMProviderManager

PROJECT_ROOT = Path(__file__).parents[1]


def test_provider_facade_contains_no_moved_owner_implementation() -> None:
    """稳定 Facade 不得重新承载已迁移的目录、发现、鉴权或运行时实现。"""
    path = PROJECT_ROOT / "app" / "agent" / "llm" / "provider.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    manager = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LLMProviderManager")
    method_names = {node.name for node in manager.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert len(source.splitlines()) < 600
    assert method_names.isdisjoint(
        {
            "_builtin_provider_specs",
            "_list_models_from_google",
            "_resolve_chatgpt_oauth",
            "_cleanup_auth_sessions_locked",
            "_resolve_cached_model_record",
        }
    )


def test_private_provider_behavior_resolves_to_single_owner_classes() -> None:
    """Facade 的私有能力必须直接解析到各自 owner，而不是保留转发副本。"""
    assert LLMProviderManager._builtin_provider_specs.__func__.__qualname__.startswith("_ProviderCatalog.")
    assert LLMProviderManager._list_models_from_google.__qualname__.startswith("_ProviderDiscovery.")
    assert LLMProviderManager._resolve_chatgpt_oauth.__qualname__.startswith("_ProviderAuth.")
    assert LLMProviderManager._cleanup_auth_sessions_locked.__qualname__.startswith("_ProviderSession.")
    assert LLMProviderManager._resolve_cached_model_record.__qualname__.startswith("_ProviderCatalog.")


def test_helper_has_no_legacy_runtime_or_discovery_fallbacks() -> None:
    """Helper 只消费注入的 Provider 运行时，不得维护第二套运行和发现实现。"""
    path = PROJECT_ROOT / "app" / "agent" / "llm" / "helper.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    helper = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LLMHelper")
    method_names = {node.name for node in helper.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert "_build_legacy_runtime" not in method_names
    assert "_get_google_models" not in method_names
    assert "_get_openai_compatible_models" not in method_names


def test_oauth_result_html_escapes_external_error_text() -> None:
    """OAuth 外部错误文本必须以纯文本渲染，不能注入标签或脚本。"""
    message = '<img src=x onerror=alert(1)><script>alert(2)</script>&"'

    result = render_auth_result_html(False, message)

    assert "<img" not in result
    assert "<script>alert(2)</script>" not in result
    assert "&lt;img src=x onerror=alert(1)&gt;" in result
    assert "&lt;script&gt;alert(2)&lt;/script&gt;&amp;&quot;" in result
