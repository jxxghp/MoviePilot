"""MoviePilot Agent 宿主策略公共内部入口。"""

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "ActionEffect": "app.agent.policy.contracts",
    "ActionPolicy": "app.agent.policy.contracts",
    "AuthSource": "app.agent.policy.contracts",
    "ConfirmationMode": "app.agent.policy.contracts",
    "ExecutionOutcome": "app.agent.policy.contracts",
    "ExecutionReceipt": "app.agent.policy.contracts",
    "MigrationState": "app.agent.policy.contracts",
    "PolicyDecision": "app.agent.policy.contracts",
    "PolicyObservation": "app.agent.policy.contracts",
    "PolicyPrincipal": "app.agent.policy.contracts",
    "PrincipalRole": "app.agent.policy.contracts",
    "PrincipalType": "app.agent.policy.contracts",
    "RecoveryMode": "app.agent.policy.contracts",
    "ResultSensitivity": "app.agent.policy.contracts",
    "ToolInvocation": "app.agent.policy.contracts",
    "ToolOrigin": "app.agent.policy.contracts",
    "ToolPolicyContext": "app.agent.policy.contracts",
    "ToolRevision": "app.agent.policy.contracts",
    "ApiOperationRoute": "app.agent.policy.api",
    "ApiOperationSpec": "app.agent.policy.api",
    "API_OPERATION_BY_ID": "app.agent.policy.api",
    "API_OPERATION_ROUTES": "app.agent.policy.api",
    "API_OPERATION_SPECS": "app.agent.policy.api",
    "list_api_operation_ids": "app.agent.policy.api",
    "resolve_api_operation": "app.agent.policy.api",
    "resolve_api_route": "app.agent.policy.api",
    "AgentToolPolicyOrchestrator": "app.agent.policy.orchestrator",
    "DEFAULT_TOOL_POLICY_ORCHESTRATOR": "app.agent.policy.orchestrator",
    "call_policy_hook": "app.agent.policy.orchestrator",
    "DEFAULT_TOOL_POLICY_REGISTRY": "app.agent.policy.registry",
    "ToolPolicyRegistry": "app.agent.policy.registry",
    "requests_system_setting_secrets": "app.agent.policy.registry",
    "REDACTED_VALUE": "app.agent.policy.sanitizer",
    "sanitize_for_host": "app.agent.policy.sanitizer",
    "stable_type_name": "app.agent.policy.sanitizer",
    "summarize_error": "app.agent.policy.sanitizer",
    "summarize_input": "app.agent.policy.sanitizer",
    "summarize_result": "app.agent.policy.sanitizer",
}


def __getattr__(name: str) -> Any:
    """首次访问公开策略对象时只加载其所属模块。"""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'app.agent.policy' has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """让惰性公开对象继续支持交互式发现。"""
    return sorted(set(globals()) | set(_EXPORT_MODULES))


__all__ = list(_EXPORT_MODULES)
