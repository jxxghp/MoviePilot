"""插件远程命令注册表。

登记由插件实例的生命周期驱动：实例进入运行态时把自己的命令登记进来，停止或停用时按
实例键回收。命令中枢在组装命令表时读取本表，因此插件停用后其命令不会残留——本表不再
交出它，下一次组装就没有它，用户再敲该命令即落到「命令不存在」。

本表只承载**插件**提供的命令。内建命令与其它来源的命令由命令中枢自己持有，三者的合并
次序由命令中枢在组装时决定，不在本表内表达。

## 跨插件同命令词的处置

同一命令词被**不同插件**声明时，本表两边都不交出，并告警一次。

判据是命令与服务实例类型不同。服务实例类型的标识指称的是同一个外部对象——``u115``
就是 115 网盘，插件声明它即「我来提供这个后端的另一份实现」，覆盖方明确意在替换实现，
因此「后登记覆盖先登记、停用即恢复」成立。命令词不指称任何共同对象：``/sync`` 在一个
插件里是同步媒体库、在另一个插件里是同步网盘，两者只是两套互不相干的语义争同一个词，
没有哪一方算「意图覆盖」，宿主无从裁决谁对。这与筛选规则标识完全同构，因此沿用同一条
处置。

按登记顺序取其一则违反「不依赖宿主内部登记顺序做静默挑选」——用户敲同一个词得到哪个
插件的行为会随插件加载顺序变化，且提示只落在日志里，用户看不到。

命名空间隔离在这里走不通：命令词是用户在聊天窗口里手打的字符串，加前缀会得到一个插件
作者没声明过、用户也预测不出的词，且前缀会挤占命令词文法本就只有 32 位的长度预算。

两边都不交出还让冲突落回安全态：争的若是内建命令词，插件声明全部作废后该词回落为内建
命令；争的若是新词，它就不存在，用户敲它得到既有的「命令不存在」提示。冲突只作废争用的
那一个命令词，双方其余命令照常生效；一方停用后另一方重新参与裁决并接手。

插件与内建同命令词的处置不在本表，见 `app.runtime.extensions.admission.command_arbitration`：
内建命令表是命令中枢自己的东西，本表看不见它。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.runtime.extensions.contract.instance import extension_id_of
from app.runtime.extensions.admission.extension_scoped import instance_precedence
from app.runtime.log import logger as default_logger


@dataclass(frozen=True)
class CommandClaim:
    """插件对一个命令词的声明及其跨插件裁决结果。

    :param cmd: 命令词
    :param plugins: 声明该命令词的插件标识，已排序
    :param owners: 与 `plugins` 一一对应的实例键
    :param effective: 该命令词的插件声明是否通过跨插件裁决，被多个插件声明时为 False
    """

    cmd: str
    plugins: Tuple[str, ...]
    owners: Tuple[str, ...]
    effective: bool


class PluginCommandRegistry:
    """按实例键登记插件提供的远程命令。"""

    def __init__(self, log: Any = default_logger) -> None:
        """创建登记表。

        :param log: 日志端口
        """
        self._lock = threading.RLock()
        self._logger = log
        # 实例键 -> {命令词: 命令定义}
        self._commands: Dict[str, Dict[str, dict]] = {}
        # 登记内容的版本号，供消费方判断自己手上的命令表是否过期
        self._revision = 0
        # 已告警过的 (命令词, 声明方插件标识元组)，避免同一冲突反复刷屏
        self._conflicts_warned: set[Tuple[str, Tuple[str, ...]]] = set()

    @property
    def revision(self) -> int:
        """返回登记内容的版本号。

        每次登记内容发生变化时递增。消费方组装出的命令表是本表某一版本的快照，版本号
        变了即代表快照过期，须重新组装——插件停用后命令要立即消失，靠的就是这个判断。

        :return: 当前版本号
        """
        with self._lock:
            return self._revision

    def register(
        self, owner: str, commands: Optional[Iterable[Tuple[str, dict]]] = None
    ) -> None:
        """按实例键整体重建该实例的命令登记。

        整体替换而不是逐条追加：调用方每次交出的都是该实例当前的全部命令，声明缩减后
        旧登记不应残留。

        :param owner: 实例键
        :param commands: (命令词, 命令定义) 序列，为空表示该实例不提供命令
        :return: 无返回值
        """
        table = {key: value for key, value in (commands or ())}
        with self._lock:
            if self._commands.get(owner, {}) == table:
                return
            if table:
                self._commands[owner] = table
            else:
                self._commands.pop(owner, None)
            self._revision += 1

    def unregister_owner(self, owner: str) -> None:
        """回收指定实例键的全部登记。

        :param owner: 实例键
        :return: 无返回值
        """
        with self._lock:
            if self._commands.pop(owner, None) is not None:
                self._revision += 1

    def clear(self) -> None:
        """清空全部登记。

        :return: 无返回值
        """
        with self._lock:
            if not self._commands:
                return
            self._commands.clear()
            self._conflicts_warned.clear()
            self._revision += 1

    def command_definitions(self) -> Dict[str, dict]:
        """交出当前生效的全部插件命令定义。

        :return: 命令词到命令定义的映射，跨插件冲突的命令词已被剔除
        """
        with self._lock:
            snapshot = {
                owner: dict(items) for owner, items in self._commands.items()
            }
        claimants = self._collect_claimants(snapshot)
        resolved: Dict[str, dict] = {}
        for cmd, owners_by_plugin in claimants.items():
            if len(owners_by_plugin) > 1:
                self._warn_conflict(cmd, owners_by_plugin)
                continue
            owner = next(iter(owners_by_plugin.values()))
            resolved[cmd] = snapshot[owner][cmd]
        return resolved

    def claims(self) -> Tuple[CommandClaim, ...]:
        """列出插件对各命令词的声明及其跨插件裁决结果。

        冲突失效的命令词同样交出：用户只在日志里见过一次告警，可见性入口要能回答
        「这个命令为什么不生效、涉及哪些插件」。结果按命令词排序，与登记先后无关。

        :return: 按命令词排序的声明元组
        """
        with self._lock:
            snapshot = {
                owner: dict(items) for owner, items in self._commands.items()
            }
        claims: List[CommandClaim] = []
        for cmd, owners_by_plugin in self._collect_claimants(snapshot).items():
            plugins = tuple(sorted(owners_by_plugin))
            claims.append(CommandClaim(
                cmd=cmd,
                plugins=plugins,
                owners=tuple(owners_by_plugin[plugin] for plugin in plugins),
                effective=len(plugins) == 1,
            ))
        return tuple(sorted(claims, key=lambda claim: claim.cmd))

    def owners(self) -> Tuple[str, ...]:
        """列出当前有登记的全部实例键。

        :return: 实例键元组
        """
        with self._lock:
            return tuple(self._commands)

    def diagnose(self) -> List[Dict[str, Any]]:
        """输出只读的登记诊断信息。

        :return: 每条登记的命令词、声明方实例键与是否因跨插件冲突失效
        """
        with self._lock:
            snapshot = {
                owner: dict(items) for owner, items in self._commands.items()
            }
        entries: List[Dict[str, Any]] = []
        for cmd, owners_by_plugin in self._collect_claimants(snapshot).items():
            conflicted = len(owners_by_plugin) > 1
            for owner in owners_by_plugin.values():
                entries.append({
                    "cmd": cmd,
                    "owner": owner,
                    "effective": not conflicted,
                })
        return entries

    @staticmethod
    def _collect_claimants(
        table: Dict[str, Dict[str, dict]]
    ) -> Dict[str, Dict[str, str]]:
        """按命令词归拢声明方，同一插件的多个实例收敛为一个。

        同一插件的多个实例声明同一命令词已由扩展级裁决在投影处消解，此处仍按同一规则
        收敛一次作为兜底：只读实例键本身，任何登记顺序都得到同一个结果。

        :param table: 实例键到「命令词到命令定义」的映射
        :return: 命令词到「插件标识到实例键」的映射
        """
        claimants: Dict[str, Dict[str, str]] = {}
        for owner, items in table.items():
            plugin_id = extension_id_of(owner)
            for cmd in items:
                owners_by_plugin = claimants.setdefault(cmd, {})
                current = owners_by_plugin.get(plugin_id)
                if current is None or instance_precedence(owner) < instance_precedence(current):
                    owners_by_plugin[plugin_id] = owner
        return claimants

    def _warn_conflict(self, cmd: str, owners_by_plugin: Dict[str, str]) -> None:
        """就一个命令词被多个插件声明打一次提示。

        :param cmd: 冲突的命令词
        :param owners_by_plugin: 插件标识到实例键的映射
        :return: 无返回值
        """
        plugin_ids = tuple(sorted(owners_by_plugin))
        seen = (cmd, plugin_ids)
        with self._lock:
            if seen in self._conflicts_warned:
                return
            self._conflicts_warned.add(seen)
        self._logger.warning(
            f"{len(plugin_ids)} 个插件声明了同一个命令 {cmd}：{list(plugin_ids)}；"
            f"命令词是用户手打的全局标识，两个插件的同名命令做的并不是同一件事，"
            f"宿主无从裁决该把它交给谁，按登记顺序取其一会让同一个词的行为随插件加载"
            f"顺序变化，因此该命令的全部插件声明一并失效"
            f"（同名的内建命令仍然生效）。"
            f"请让其中一方改用别的命令词，或停用其中一个插件"
        )


plugin_command_registry = PluginCommandRegistry()
