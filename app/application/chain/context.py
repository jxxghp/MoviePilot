"""Chain 兼容门面所需运行时依赖的显式上下文。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from app.application.messaging.message import MessageHelper, MessageQueueManager
from app.db.oper.message import MessageOper
from app.runtime.cache import AsyncFileCache, FileCache
from app.runtime.events import EventManager
from app.runtime.extensions.module_manager import ModuleManager
from app.runtime.extensions.plugin_manager import PluginManager


MessageQueueFactory = Callable[[Callable[..., Any]], Any]
ChainRuntimeContextProvider = Callable[[], "ChainRuntimeContext"]


@dataclass(frozen=True, slots=True)
class ChainRuntimeContext:
    """集中声明 Chain 调度、事件、消息和缓存所需的最小运行时对象。"""

    module_manager: Any
    plugin_manager: Any
    event_manager: Any
    message_oper: Any
    message_helper: Any
    file_cache: Any
    async_file_cache: Any
    message_queue_factory: MessageQueueFactory


def build_default_chain_runtime_context() -> ChainRuntimeContext:
    """按旧构造规则创建上下文，同时复用各管理器既有单例身份。"""
    return ChainRuntimeContext(
        module_manager=ModuleManager(),
        plugin_manager=PluginManager(),
        event_manager=EventManager(),
        message_oper=MessageOper(),
        message_helper=MessageHelper(),
        file_cache=FileCache(),
        async_file_cache=AsyncFileCache(),
        message_queue_factory=lambda callback: MessageQueueManager(
            send_callback=callback
        ),
    )


_context_provider: ChainRuntimeContextProvider = build_default_chain_runtime_context


def configure_chain_runtime_context_provider(
    provider: Optional[ChainRuntimeContextProvider],
) -> None:
    """由组合根替换 Chain 上下文来源；传入空值恢复兼容默认值。"""
    global _context_provider
    _context_provider = provider or build_default_chain_runtime_context


def get_chain_runtime_context() -> ChainRuntimeContext:
    """返回当前组合根提供的 Chain 运行上下文。"""
    return _context_provider()
