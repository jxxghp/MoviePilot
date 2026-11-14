"""
LLM helper 与 provider 实现之间的运行时端口。
"""

from collections.abc import Callable
from typing import Any, Protocol


class LLMProviderRuntimePort(Protocol):
    """
    声明 LLM helper 与管理 API 共用的 provider 运行时能力。
    """

    def resolve_cached_model_metadata(self, **kwargs: Any) -> dict[str, Any] | None:
        """
        从本地目录缓存解析模型元数据。

        :param kwargs: provider_id、model_id 及可选的 base_url、base_url_preset_id，用于定位缓存目录
        :return: 已缓存的模型元数据；没有匹配项时为 None，不触发远端刷新
        """
        ...

    async def resolve_runtime(self, **kwargs: Any) -> dict[str, Any]:
        """
        解析创建模型客户端所需的统一运行时参数。

        :param kwargs: provider_id、model 及可选的 api_key、base_url、base_url_preset_id、user_agent、use_proxy
        :return: 等待认证与地址解析完成后的统一客户端参数，包含实际运行时、模型、凭据及网络设置
        """
        ...

    def create_bedrock_client(self, *args: Any, **kwargs: Any) -> Any:
        """
        创建带统一认证和网络配置的 Bedrock 客户端。

        :param args: 按顺序传入服务名、区域和已解析凭据，可继续传入地址、代理开关及读取超时
        :param kwargs: 与位置参数对应的命名参数；read_timeout 以秒计，None 表示默认超时
        :return: 按统一认证和网络策略配置的 boto3 客户端
        """
        ...

    async def list_models(self, **kwargs: Any) -> list[dict[str, Any]]:
        """
        返回 provider 可用的模型目录。

        :param kwargs: provider_id 及可选认证、地址、代理配置；force_refresh 控制是否刷新目录
        :return: 等待目录查询完成后的标准化模型信息列表
        """
        ...

    def resolve_model_list_base_url(self, **kwargs: Any) -> str | None:
        """
        解析兼容接口用于查询模型列表的基础地址。

        :param kwargs: provider_id、base_url 及可选 base_url_preset_id，用于选择模型目录地址
        :return: 解析后的目录基础地址；提供商没有可用地址时为 None
        """
        ...

    async def provider_manage(
        self,
        provider: str,
        action: str,
        **params: Any,
    ) -> dict[str, Any]:
        """
        执行与具体提供商无关的统一管理动作。

        :param provider: 目标提供商的稳定标识
        :param action: 统一管理动作，决定查询目录、发起授权或管理连接等操作
        :param params: 对应动作的参数，如认证方式、回调地址、会话标识或临时连接配置
        :return: 等待动作完成后的 success、message、data 结果，公开内容应按宿主规则脱敏
        """
        ...

    async def handle_chatgpt_callback(
        self,
        provider_id: str,
        code: str | None,
        state: str | None,
        error: str | None,
        error_description: str | None,
    ) -> tuple[bool, str]:
        """
        完成 ChatGPT OAuth 回调并返回公开结果。

        :param provider_id: 发起浏览器授权的提供商标识
        :param code: 授权服务返回的授权码；失败回调可为 None
        :param state: 与临时授权会话关联的状态标识；缺失时无法完成匹配
        :param error: 授权服务返回的错误码；成功回调为 None
        :param error_description: 授权错误的补充说明；没有说明时为 None
        :return: 等待回调处理后的成功标记和面向用户的结果说明；成功时更新授权凭据和临时状态
        """
        ...


LLMProviderRuntimeFactory = Callable[[], LLMProviderRuntimePort]
_provider_runtime_factory: LLMProviderRuntimeFactory | None = None


def register_llm_provider_runtime(
        factory: LLMProviderRuntimeFactory | None,
) -> LLMProviderRuntimeFactory | None:
    """
    注册 provider 运行时工厂，并返回先前工厂供隔离测试恢复。
    """
    global _provider_runtime_factory
    previous = _provider_runtime_factory
    _provider_runtime_factory = factory
    return previous


def resolve_llm_provider_runtime() -> LLMProviderRuntimePort:
    """
    解析已组装的 provider 运行时，未注册时给出明确边界错误。
    """
    if _provider_runtime_factory is None:
        raise RuntimeError("LLM provider 运行时尚未由启动层完成组装")
    return _provider_runtime_factory()
