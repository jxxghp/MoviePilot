"""LLM helper 与 provider 实现之间的运行时端口。"""

from collections.abc import Callable
from typing import Any, Protocol


class LLMProviderRuntimePort(Protocol):
    """声明 LLM helper 与管理 API 共用的 provider 运行时能力。"""

    def resolve_cached_model_metadata(self, **kwargs: Any) -> dict[str, Any] | None:
        """从本地目录缓存解析模型元数据。"""
        ...

    async def resolve_runtime(self, **kwargs: Any) -> dict[str, Any]:
        """解析创建模型客户端所需的统一运行时参数。"""
        ...

    def create_bedrock_client(self, *args: Any, **kwargs: Any) -> Any:
        """创建带统一认证和网络配置的 Bedrock 客户端。"""
        ...

    async def list_models(self, **kwargs: Any) -> list[dict[str, Any]]:
        """返回 provider 可用的模型目录。"""
        ...

    def resolve_model_list_base_url(self, **kwargs: Any) -> str | None:
        """解析兼容接口用于查询模型列表的基础地址。"""
        ...

    async def provider_manage(
        self,
        provider: str,
        action: str,
        **params: Any,
    ) -> dict[str, Any]:
        """执行与具体提供商无关的统一管理动作。"""
        ...

    async def handle_chatgpt_callback(
        self,
        provider_id: str,
        code: str | None,
        state: str | None,
        error: str | None,
        error_description: str | None,
    ) -> tuple[bool, str]:
        """完成 ChatGPT OAuth 回调并返回公开结果。"""
        ...


LLMProviderRuntimeFactory = Callable[[], LLMProviderRuntimePort]
_provider_runtime_factory: LLMProviderRuntimeFactory | None = None


def register_llm_provider_runtime(
        factory: LLMProviderRuntimeFactory | None,
) -> LLMProviderRuntimeFactory | None:
    """注册 provider 运行时工厂，并返回先前工厂供隔离测试恢复。"""
    global _provider_runtime_factory
    previous = _provider_runtime_factory
    _provider_runtime_factory = factory
    return previous


def resolve_llm_provider_runtime() -> LLMProviderRuntimePort:
    """解析已组装的 provider 运行时，未注册时给出明确边界错误。"""
    if _provider_runtime_factory is None:
        raise RuntimeError("LLM provider 运行时尚未由启动层完成组装")
    return _provider_runtime_factory()
