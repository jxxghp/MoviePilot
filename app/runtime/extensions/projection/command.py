"""插件命令表的组装。

插件命令登记在 `app.runtime.extensions.registry.command`，跨插件同命令词的裁决也在表内
完成。与内建命令的同词争用只有本模块看得见——内建命令表由命令中枢持有，注册表看不到它
——因此在这里按接管意图裁决一次，未声明接管的同名插件命令不进命令表，用户敲这个词仍得到
内建行为。

命令表是注册表某一版本的快照。插件停用或卸载后其命令必须立刻消失，而广播注册是异步的、
也未必每次都被触发，因此取用前比对登记版本号：版本变了即代表快照过期，重新组装一次。
用户敲已卸载插件的命令由此落到「命令不存在」，而不是调用到已卸载实例上。

本模块只把登记结果投影成一张表，不改变任何登记内容。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from app.runtime.extensions.admission.command_arbitration import BuiltinCommandArbiter
from app.runtime.extensions.registry.command import (
    CommandClaim,
    PluginCommandRegistry,
    plugin_command_registry,
)


class PluginCommandTable:
    """按插件命令登记与内建命令词组装出可查表的插件命令。"""

    def __init__(
        self,
        builtin_command_words: Callable[[], Iterable[str]],
        event_sender: Callable[..., None],
        registry: Optional[PluginCommandRegistry] = None,
        arbiter: Optional[BuiltinCommandArbiter] = None,
    ) -> None:
        """创建插件命令表。

        :param builtin_command_words: 交出当前内建命令词的可调用对象，裁决同词争用时读取
        :param event_sender: 未声明可调用实现的命令改走事件分发时使用的发送器
        :param registry: 插件命令登记表，缺省使用进程级登记表
        :param arbiter: 插件与内建同词的裁决器，缺省新建一个
        """
        self._builtin_command_words = builtin_command_words
        self._event_sender = event_sender
        self._registry = registry if registry is not None else plugin_command_registry
        self._arbiter = arbiter if arbiter is not None else BuiltinCommandArbiter()
        self._lock = threading.RLock()
        # 通过裁决的插件命令，命令词到命令表条目
        self._commands: Dict[str, dict] = {}
        # 与内建命令同名却未声明接管意图、已作废的插件命令
        self._declined: Dict[str, dict] = {}
        # 已组装内容对应的登记版本号，-1 表示尚未按登记组装过
        self._revision = -1

    @property
    def commands(self) -> Dict[str, dict]:
        """通过裁决的插件命令表。

        :return: 命令词到命令表条目的映射
        """
        with self._lock:
            return self._commands

    @property
    def declined(self) -> Dict[str, dict]:
        """撞上内建命令且未声明接管意图而作废的插件命令表。

        :return: 命令词到命令表条目的映射
        """
        with self._lock:
            return self._declined

    def claims(self) -> Tuple[CommandClaim, ...]:
        """列出插件对各命令词的声明及其跨插件裁决结果。

        :return: 按命令词排序的声明元组
        """
        return self._registry.claims()

    def refresh(self) -> bool:
        """登记版本变化时重新组装命令表。

        :return: 本次调用是否重新组装过
        """
        if self._registry.revision == self._revision:
            return False
        with self._lock:
            if self._registry.revision == self._revision:
                return False
            self.rebuild()
            return True

    def rebuild(self) -> None:
        """无条件按当前登记重新组装命令表。

        :return: 无返回值
        """
        with self._lock:
            revision = self._registry.revision
            definitions = self._registry.command_definitions()
            arbitration = self._arbiter.arbitrate(
                definitions, self._builtin_command_words()
            )
            self._declined = {
                cmd: {
                    "pid": command.get("pid"),
                    "description": command.get("desc"),
                    "category": command.get("category"),
                }
                for cmd, command in arbitration.declined.items()
            }
            self._commands = {
                cmd: self._entry(command)
                for cmd, command in arbitration.effective.items()
            }
            self._revision = revision

    def _entry(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """把一条通过裁决的插件命令定义投影为命令表条目。

        声明了可调用实现的直接调用实现；只声明事件类型的改走事件分发。

        :param command: 插件命令定义
        :return: 命令表条目
        """
        impl = command.get("impl")
        data = command.get("data") or {}
        if callable(impl):
            func, payload = impl, {"data": data}
        else:
            func, payload = self._event_sender, {
                "etype": command.get("event"),
                "data": data,
            }
        return {
            "pid": command.get("pid"),
            "func": func,
            "description": command.get("desc"),
            "category": command.get("category"),
            "show": command.get("show", True),
            "data": payload,
        }
