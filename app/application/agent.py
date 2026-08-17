"""Agent 编排服务门面。

chain 层需要触发 Agent 后台任务、渲染提示词、查询模型能力时统一经本模块调用。
具体实现由 startup 组合根注册，形成依赖倒置：

    chain -> application.agent <- startup -> agent

门面保存 provider 而非重量级实现对象，注册本身不会物化 Agent、LLM 或工具树。
本模块禁止静态或函数内导入 app.agent，否则会重新形成跨层循环依赖。
"""

from typing import Any, Callable, Optional

Provider = Callable[[], Any]

# provider 注册表由 startup/agent_initializer 在组合根装配。
_agent_manager_provider: Optional[Provider] = None
_running_agent_manager_provider: Optional[Provider] = None
_prompt_manager_provider: Optional[Provider] = None
_agent_capability_manager_provider: Optional[Provider] = None
_llm_helper_provider: Optional[Provider] = None
_manual_redo_prompt_builder_provider: Optional[Provider] = None
_skill_helper_provider: Optional[Provider] = None


def register_agent_service_providers(
        *,
        agent_manager_provider: Provider,
        running_agent_manager_provider: Provider,
        prompt_manager_provider: Provider,
        capability_manager_provider: Provider,
        llm_helper_provider: Provider,
        manual_redo_prompt_builder_provider: Provider,
        skill_helper_provider: Provider,
) -> None:
    """注册 Agent 服务 provider，保持组合根装配阶段零重量实现导入。"""
    global _agent_manager_provider, _running_agent_manager_provider
    global _prompt_manager_provider, _agent_capability_manager_provider
    global _llm_helper_provider, _manual_redo_prompt_builder_provider
    global _skill_helper_provider
    _agent_manager_provider = agent_manager_provider
    _running_agent_manager_provider = running_agent_manager_provider
    _prompt_manager_provider = prompt_manager_provider
    _agent_capability_manager_provider = capability_manager_provider
    _llm_helper_provider = llm_helper_provider
    _manual_redo_prompt_builder_provider = manual_redo_prompt_builder_provider
    _skill_helper_provider = skill_helper_provider


def register_agent_services(
        agent_manager: Any,
        prompt_manager: Any,
        capability_manager: Any,
        llm_helper: Any,
        manual_redo_prompt_builder: Optional[Callable[[Any], str]] = None,
        skill_helper: Any = None,
) -> None:
    """兼容直接对象注入；生产组合根应注册惰性 provider。"""
    register_agent_service_providers(
        agent_manager_provider=lambda: agent_manager,
        running_agent_manager_provider=lambda: agent_manager,
        prompt_manager_provider=lambda: prompt_manager,
        capability_manager_provider=lambda: capability_manager,
        llm_helper_provider=lambda: llm_helper,
        manual_redo_prompt_builder_provider=lambda: manual_redo_prompt_builder,
        skill_helper_provider=lambda: skill_helper,
    )


def _resolve(provider: Optional[Provider], service_name: str) -> Any:
    """解析已注册服务；缺少组合根装配时给出稳定错误。"""
    if provider is None:
        raise RuntimeError(
            f"Agent 服务 {service_name} 未注册："
            "请先导入 app.startup.agent_initializer 完成组合根装配"
        )
    return provider()


def get_agent_manager() -> Any:
    """返回 canonical AgentManager；调用可能触发实现物化。"""
    return _resolve(_agent_manager_provider, "agent_manager")


def get_running_agent_manager() -> Any | None:
    """返回已进入 RUNNING 的 AgentManager，不触发实现物化。"""
    return _resolve(_running_agent_manager_provider, "running_agent_manager")


def get_prompt_manager() -> Any:
    """按需返回提示词管理器。"""
    return _resolve(_prompt_manager_provider, "prompt_manager")


def get_skill_helper() -> Any:
    """按需返回 SkillHelper 单例，调用可能触发实现物化。"""
    return _resolve(_skill_helper_provider, "skill_helper")


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
