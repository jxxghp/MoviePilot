"""Agent 编排服务门面。

chain 层需要触发 Agent 后台任务、渲染提示词、查询模型能力时统一经本模块调用。
具体实现由 startup 组合根注册，形成依赖倒置：

    chain -> application.agent <- startup -> agent

门面保存 provider 而非重量级实现对象，注册本身不会物化 Agent、LLM 或工具树。
本模块禁止静态或函数内导入 app.agent，否则会重新形成跨层循环依赖。
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

from app.application.agenttask import AgentTaskRepository
from app.application.history import DownloadHistoryRepository, TransferHistoryRepository
from app.application.messaging.chat import (
    AgentChatPersistenceService,
    AgentChatService,
)
from app.application.plugin.data import PluginDataQueryRepository
from app.application.security.user import ChainUserRepository
from app.application.site.contract import SiteRepository
from app.application.subscription.contract import (
    SubscriptionHistoryQueryPort,
    SubscriptionRepository,
)
from app.application.transfer.execution import TransferExecutionRepository

if TYPE_CHECKING:
    from app.application.rules import AsyncRuleGroupMutationService
    from app.application.subscription.delete import DeleteSubscribeScope
    from app.application.subscription.mutation import SubscriptionMutationScope


@dataclass(frozen=True, slots=True)
class AgentDataContext:
    """由启动组合根构造并注入 Agent 运行面的类型化数据能力。"""

    chat: AgentChatService
    chat_persistence: AgentChatPersistenceService
    tasks: AgentTaskRepository
    users: ChainUserRepository
    sites: SiteRepository
    subscriptions: SubscriptionRepository
    subscription_mutation_scope: SubscriptionMutationScope
    subscription_delete_scope: DeleteSubscribeScope
    async_rule_group_mutation_scope: Callable[
        [], AbstractAsyncContextManager[AsyncRuleGroupMutationService]
    ]
    subscription_history: SubscriptionHistoryQueryPort
    transfer_history: TransferHistoryRepository
    transfer_execution: TransferExecutionRepository
    download_history: DownloadHistoryRepository
    plugin_data: PluginDataQueryRepository

Provider = Callable[[], Any]

# provider 注册表由 startup/initializers/agent.py 在组合根装配。
_agent_manager_provider: Optional[Provider] = None
_running_agent_manager_provider: Optional[Provider] = None
_prompt_manager_provider: Optional[Provider] = None
_agent_capability_manager_provider: Optional[Provider] = None
_llm_helper_provider: Optional[Provider] = None
_manual_redo_prompt_builder_provider: Optional[Provider] = None


def register_agent_service_providers(
        *,
        agent_manager_provider: Provider,
        running_agent_manager_provider: Provider,
        prompt_manager_provider: Provider,
        capability_manager_provider: Provider,
        llm_helper_provider: Provider,
        manual_redo_prompt_builder_provider: Provider,
) -> None:
    """注册 Agent 服务 provider，保持组合根装配阶段零重量实现导入。"""
    global _agent_manager_provider, _running_agent_manager_provider
    global _prompt_manager_provider, _agent_capability_manager_provider
    global _llm_helper_provider, _manual_redo_prompt_builder_provider
    _agent_manager_provider = agent_manager_provider
    _running_agent_manager_provider = running_agent_manager_provider
    _prompt_manager_provider = prompt_manager_provider
    _agent_capability_manager_provider = capability_manager_provider
    _llm_helper_provider = llm_helper_provider
    _manual_redo_prompt_builder_provider = manual_redo_prompt_builder_provider


def register_agent_services(
        agent_manager: Any,
        prompt_manager: Any,
        capability_manager: Any,
        llm_helper: Any,
        manual_redo_prompt_builder: Optional[Callable[[Any], str]] = None,
) -> None:
    """兼容直接对象注入；生产组合根应注册惰性 provider。"""
    register_agent_service_providers(
        agent_manager_provider=lambda: agent_manager,
        running_agent_manager_provider=lambda: agent_manager,
        prompt_manager_provider=lambda: prompt_manager,
        capability_manager_provider=lambda: capability_manager,
        llm_helper_provider=lambda: llm_helper,
        manual_redo_prompt_builder_provider=lambda: manual_redo_prompt_builder,
    )


def _resolve(provider: Optional[Provider], service_name: str) -> Any:
    """解析已注册服务；缺少组合根装配时给出稳定错误。"""
    if provider is None:
        raise RuntimeError(
            f"Agent 服务 {service_name} 未注册："
            "请先导入 app.startup.initializers.agent 完成组合根装配"
        )
    return provider()


def get_agent_manager() -> Any:
    """返回 canonical AgentManager；调用可能触发实现物化。"""
    return _resolve(_agent_manager_provider, "agent_manager")


def get_running_agent_manager() -> Any | None:
    """返回已进入 RUNNING 的 AgentManager，不触发实现物化。"""
    if _running_agent_manager_provider is None:
        return None
    return _resolve(_running_agent_manager_provider, "running_agent_manager")


def get_prompt_manager() -> Any:
    """按需返回提示词管理器。"""
    return _resolve(_prompt_manager_provider, "prompt_manager")


def supports_image_input(
        provider: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        base_url_preset: Optional[str] = None,
) -> bool:
    """判断当前模型是否启用了图片输入能力。"""
    llm_helper = _resolve(_llm_helper_provider, "llm_helper")
    return llm_helper.supports_image_input(
        provider=provider,
        model=model,
        base_url=base_url,
        base_url_preset=base_url_preset,
    )


def is_audio_input_available() -> bool:
    """判断语音输入能力是否可用。"""
    capability_manager = _resolve(
        _agent_capability_manager_provider,
        "agent_capability_manager",
    )
    return capability_manager.is_audio_input_available()


def transcribe_audio(content: bytes, filename: str = "input.ogg") -> Optional[str]:
    """把音频内容转写为文本。"""
    capability_manager = _resolve(
        _agent_capability_manager_provider,
        "agent_capability_manager",
    )
    return capability_manager.transcribe_audio(content, filename=filename)


def build_manual_redo_prompt(history: Any) -> str:
    """构造整理记录 AI 重新整理提示词（builder 由 agent 层注册）。"""
    builder = _resolve(
        _manual_redo_prompt_builder_provider,
        "manual_redo_prompt_builder",
    )
    if builder is None:
        raise RuntimeError("整理记录重新整理提示词构建器未注册")
    return builder(history)
