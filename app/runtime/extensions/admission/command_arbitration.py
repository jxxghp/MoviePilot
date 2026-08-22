"""插件命令与内建命令同词时的裁决。

命令表由内建、插件、其它三处来源合并而成，合并写法本身决定了同一命令词最终归谁。本
模块只回答其中一问：插件声明了一个内建已有的命令词，该交给谁。

## 判据

已有两条先例，取哪一条要看**标识本身是否承载了「意图替换」**：

- **后登记覆盖**（存储与服务实例类型）：标识指称同一个外部对象——``u115`` 就是 115
  网盘，插件声明它即「我来提供这个后端的另一份实现」，声明标识本身就是覆盖意图。
- **双方一并失效**（筛选规则、跨插件命令）：标识不指称任何共同对象，``/sync`` 在一个
  插件里是同步媒体库、在另一个插件里是同步网盘，没有哪一方算意图覆盖。

插件与内建同命令词落在两者之间。一方面，内建命令词是宿主自己发布的、有文档的稳定标识，
插件用插件增强 ``/subscribes`` 是正当诉求，说「没有哪一方算意图覆盖」不成立；另一方面，
插件作者也可能压根不知道宿主已有这个词，撞车与接管从命令词本身分辨不出来。

因此裁决落在「**接管意图须显式声明**」：声明了 ``overrides_builtin`` 即按接管处置，
插件命令生效、内建命令被压住；没声明就是撞车，该条插件命令作废、内建命令保持生效。
这与「后登记覆盖」是同一条原理——覆盖成立的前提是覆盖意图明确——只是命令词自己带不出
这个意图，就单列一个字段把它说出来。

不取另外两条的理由：

- **一律双废**（连内建一起废）会让任意一个插件随手起的命令词打掉宿主自己的 ``/restart``
  ——插件能删掉宿主功能，比现状更糟，且冲突不再落回安全态。
- **一律内建胜出**安全但把「用插件增强内建命令」这条正当诉求整个堵死，且插件那条命令是
  被静默丢掉的，作者与用户都不知道发生了什么。

无论哪一支都不是「按加载顺序取一个」：结果只取决于声明内容，与插件加载先后无关。

## 与跨插件裁决的次序

跨插件裁决在前：同一命令词被两个插件争用时双方一并失效，剩不下任何插件声明，本裁决也
就无从发生，内建命令照常生效。接管意图不参与跨插件裁决——两个插件都声称要接管
``/version``，宿主依然无从判断该让谁接管。

## 废弃钩子

``get_command()`` 报不出接管意图，其条目恒按撞车处置。该钩子已在废弃期，而静默盖掉内建
命令正是本模块要消除的那件事，不为它开例外。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Tuple

from app.runtime.log import logger as default_logger

# 命令来源层标识，供命令表组装与来源可见性共用一份
BUILTIN_LAYER = "builtin"
PLUGIN_LAYER = "plugin"
OTHER_LAYER = "other"


@dataclass(frozen=True)
class BuiltinOverlapArbitration:
    """插件命令与内建命令同词裁决的结果。

    :param effective: 通过裁决的插件命令，命令词到命令定义
    :param overriding: 显式声明接管、因而压住同名内建命令的命令词，已排序
    :param declined: 撞上内建命令且未声明接管意图而作废的插件命令，命令词到命令定义
    """

    effective: Dict[str, dict] = field(default_factory=dict)
    overriding: Tuple[str, ...] = ()
    declined: Dict[str, dict] = field(default_factory=dict)


class BuiltinCommandArbiter:
    """裁决插件命令与内建命令的同词争用，并就作废的声明告警。"""

    def __init__(self, log: Any = default_logger) -> None:
        """创建裁决器。

        :param log: 日志端口
        """
        self._lock = threading.RLock()
        self._logger = log
        # 已告警过的 (命令词, 声明方实例键)，避免同一撞车反复刷屏
        self._declined_warned: set[Tuple[str, str]] = set()

    def arbitrate(
        self,
        plugin_commands: Mapping[str, dict],
        builtin_commands: Iterable[str],
    ) -> BuiltinOverlapArbitration:
        """按接管意图裁决插件命令与内建命令的同词争用。

        :param plugin_commands: 已通过跨插件裁决的插件命令，命令词到命令定义
        :param builtin_commands: 内建命令词集合
        :return: 裁决结果
        """
        builtin = set(builtin_commands or ())
        effective: Dict[str, dict] = {}
        declined: Dict[str, dict] = {}
        overriding: list[str] = []
        for cmd, definition in plugin_commands.items():
            if cmd not in builtin:
                effective[cmd] = definition
                continue
            if definition.get("overrides_builtin"):
                effective[cmd] = definition
                overriding.append(cmd)
                continue
            declined[cmd] = definition
            self._warn_declined(cmd, definition)
        return BuiltinOverlapArbitration(
            effective=effective,
            overriding=tuple(sorted(overriding)),
            declined=declined,
        )

    def _warn_declined(self, cmd: str, definition: Mapping[str, Any]) -> None:
        """就一条撞上内建命令而作废的插件声明打一次提示。

        :param cmd: 撞车的命令词
        :param definition: 该条插件命令定义
        :return: 无返回值
        """
        owner = str(definition.get("pid") or "")
        seen = (cmd, owner)
        with self._lock:
            if seen in self._declined_warned:
                return
            self._declined_warned.add(seen)
        self._logger.warning(
            f"插件[{owner}]声明的命令 {cmd} 与内建命令同名，且未声明接管意图，"
            f"该条插件命令已作废，{cmd} 仍执行内建行为。"
            f"命令词是用户手打的全局标识，插件与内建同名既可能是有意接管、也可能只是"
            f"撞车，宿主分辨不出来，静默交给插件会让用户敲一个自以为是内建的命令却"
            f"执行了别的东西。若确实意在接管，请在 CommandDeclaration 上声明 "
            f"overrides_builtin=True；否则请改用别的命令词"
        )
