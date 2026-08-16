"""Agent 编排服务门面。

chain 层需要触发 Agent 后台任务、渲染提示词、查询模型能力时统一经本模块调用。
具体实现由 app.agent 在启动时注册，形成依赖倒置：

    chain -> application.agent <- agent（startup 在导入期注册）

静态依赖图上 application 不依赖 agent，agent 作为入口层向 application
注册实现，从而拆除 chain <-> agent 的互指环。

注意：本模块禁止静态导入 app.agent 下的任何模块（含函数内导入），
否则会形成 agent -> chain -> application -> agent 的新环。
未注册时的兜底注册由 startup/agent_initializer 在导入期完成。
"""

from typing import Any, Callable, Optional

# 注册表：启动期由 startup/agent_initializer 填充。
_agent_manager: Any = None
_prompt_manager: Any = None
_agent_capability_manager: Any = None
_llm_helper: Any = None
_manual_redo_prompt_builder: Optional[Callable[[Any], str]] = None


def register_agent_services(
        agent_manager: Any,
        prompt_manager: Any,
        capability_manager: Any,
        llm_helper: Any,
        manual_redo_prompt_builder: Optional[Callable[[Any], str]] = None,
) -> None:
    """注册 Agent 服务实现（由 startup 组合根在导入期调用）。"""
    global _agent_manager, _prompt_manager, _agent_capability_manager, _llm_helper
    global _manual_redo_prompt_builder
    _agent_manager = agent_manager
    _prompt_manager = prompt_manager
    _agent_capability_manager = capability_manager
    _llm_helper = llm_helper
    _manual_redo_prompt_builder = manual_redo_prompt_builder


def _ensure_registered() -> None:
    """校验 Agent 服务已注册。

    正常启动路径由 startup/agent_initializer 在导入期注册；未注册时
    直接抛出带指引的错误，避免在此处静态导入 app.agent 破坏依赖方向。
    """
    if _agent_manager is None:
        raise RuntimeError(
            "Agent 服务未注册：请先导入 app.startup.agent_initializer 完成组合根装配"
        )


def get_agent_manager() -> Any:
    """返回 AgentManager 单例。"""
    _ensure_registered()
    return _agent_manager


def get_prompt_manager() -> Any:
    """返回提示词管理器。"""
    _ensure_registered()
    return _prompt_manager


def supports_image_input(
        provider: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        base_url_preset: Optional[str] = None,
) -> bool:
    """判断当前模型是否启用了图片输入能力。"""
    _ensure_registered()
    return _llm_helper.supports_image_input(
        provider=provider,
        model=model,
        base_url=base_url,
        base_url_preset=base_url_preset,
    )


def is_audio_input_available() -> bool:
    """判断语音输入能力是否可用。"""
    _ensure_registered()
    return _agent_capability_manager.is_audio_input_available()


def transcribe_audio(content: bytes, filename: str = "input.ogg") -> Optional[str]:
    """把音频内容转写为文本。"""
    _ensure_registered()
    return _agent_capability_manager.transcribe_audio(content, filename=filename)


def build_manual_redo_prompt(history: Any) -> str:
    """构造整理记录 AI 重新整理提示词（builder 由 agent 层注册）。"""
    _ensure_registered()
    if _manual_redo_prompt_builder is None:
        raise RuntimeError("整理记录重新整理提示词构建器未注册")
    return _manual_redo_prompt_builder(history)
