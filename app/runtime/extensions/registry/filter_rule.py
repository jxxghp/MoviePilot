"""插件筛选规则与筛选规则组注册表。

登记由插件实例的生命周期驱动：实例进入运行态时把自己声明的规则与规则组登记进来，
停止或停用时按实例键回收。规则引擎在组装运行期规则集时读取本表，因此插件停用后
其规则不会残留——本表不再交出它，下一次组装就没有它。

本表只承载**插件**提供的规则。内建规则是领域常量，用户自定义规则来自系统配置，
三者的合并次序（内建 < 插件 < 用户）由规则引擎在组装时决定，不在本表内表达。

## 跨插件同标识的处置

同一标识被**不同插件**声明时，本表两边都不交出，并告警一次。

判据是规则与存储后端不同：存储后端是实现，「后登记覆盖先登记」成立，因为覆盖方
通常明确意在替换内建实现；而规则是数据，两个插件各自的 ``4KHDR`` 只是两套互不相干
的语义争同一个名字，没有哪一方算「意图覆盖」，宿主无从裁决谁对。按登记顺序取一个
则违反「不依赖宿主内部登记顺序做静默挑选」——用户看到的筛选行为会随插件加载顺序
变化，且毫无提示。

命名空间隔离在这里走不通：规则标识要作为原子进入规则串语法，该语法只接受字母与
数字，容不下任何分隔符，加前缀会得到一个插件作者没声明过、用户也预测不出的标识。

两边都不交出的附带好处是冲突落回安全态：冲突的若是内建标识，插件声明全部作废后
该标识回落为内建定义；冲突的若是新标识，它就不存在，用户在规则组里引用它时会得到
既有的「规则不存在」日志。用户自定义规则不受影响——它排在最后，永远赢。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.runtime.extensions.contract.instance import extension_id_of
from app.runtime.extensions.admission.extension_scoped import instance_precedence
from app.runtime.log import logger as default_logger

# 标识种类：筛选规则
RULE_KIND = "rule"

# 标识种类：筛选规则组
RULE_GROUP_KIND = "rule_group"


@dataclass(frozen=True, slots=True)
class FilterRuleClaim:
    """插件对一个规则标识或规则组名的声明及其裁决结果。

    :param kind: 标识种类，取 `RULE_KIND` 或 `RULE_GROUP_KIND`
    :param identity: 规则标识或规则组名
    :param plugins: 声明该标识的插件标识，已排序
    :param owners: 与 `plugins` 一一对应的实例键
    :param effective: 该标识的插件声明是否生效，被多个插件声明时为 False
    """

    kind: str
    identity: str
    plugins: Tuple[str, ...]
    owners: Tuple[str, ...]
    effective: bool


class PluginFilterRuleRegistry:
    """按实例键登记插件提供的筛选规则与筛选规则组。"""

    def __init__(self, log: Any = default_logger) -> None:
        """创建登记表。

        :param log: 日志端口
        """
        self._lock = threading.RLock()
        self._logger = log
        # 实例键 -> {规则标识: 规则定义}
        self._rules: Dict[str, Dict[str, dict]] = {}
        # 实例键 -> {规则组名: 规则组定义}
        self._groups: Dict[str, Dict[str, dict]] = {}
        # 登记内容的版本号，供消费方判断自己手上的组装结果是否过期
        self._revision = 0
        # 已告警过的 (标识种类, 标识, 声明方插件标识元组)，避免同一冲突反复刷屏
        self._conflicts_warned: set[Tuple[str, str, Tuple[str, ...]]] = set()

    @property
    def revision(self) -> int:
        """返回登记内容的版本号。

        每次登记内容发生变化时递增。消费方组装出的规则集是本表某一版本的快照，
        版本号变了即代表快照过期，须重新组装。

        :return: 当前版本号
        """
        with self._lock:
            return self._revision

    def register(
        self,
        owner: str,
        rules: Optional[Iterable[Tuple[str, dict]]] = None,
        groups: Optional[Iterable[Tuple[str, dict]]] = None,
    ) -> None:
        """按实例键整体重建该实例的规则与规则组登记。

        整体替换而不是逐条追加：调用方每次交出的都是该实例当前的全部声明，声明
        缩减后旧登记不应残留。

        :param owner: 实例键
        :param rules: (规则标识, 规则定义) 序列，为空表示该实例不提供规则
        :param groups: (规则组名, 规则组定义) 序列，为空表示该实例不提供规则组
        :return: 无返回值
        """
        rule_table = {key: value for key, value in (rules or ())}
        group_table = {key: value for key, value in (groups or ())}
        with self._lock:
            if (
                self._rules.get(owner, {}) == rule_table
                and self._groups.get(owner, {}) == group_table
            ):
                return
            if rule_table:
                self._rules[owner] = rule_table
            else:
                self._rules.pop(owner, None)
            if group_table:
                self._groups[owner] = group_table
            else:
                self._groups.pop(owner, None)
            self._revision += 1

    def unregister_owner(self, owner: str) -> None:
        """回收指定实例键的全部登记。

        :param owner: 实例键
        :return: 无返回值
        """
        with self._lock:
            removed = self._rules.pop(owner, None), self._groups.pop(owner, None)
            if any(item is not None for item in removed):
                self._revision += 1

    def clear(self) -> None:
        """清空全部登记。

        :return: 无返回值
        """
        with self._lock:
            if not self._rules and not self._groups:
                return
            self._rules.clear()
            self._groups.clear()
            self._conflicts_warned.clear()
            self._revision += 1

    def rule_definitions(self) -> Dict[str, dict]:
        """交出当前生效的全部插件规则定义。

        :return: 规则标识到规则定义的映射，跨插件冲突的标识已被剔除
        """
        return self._resolve(self._rules, subject="筛选规则", hook="provides_filter_rules")

    def rule_group_definitions(self) -> Dict[str, dict]:
        """交出当前生效的全部插件规则组定义。

        :return: 规则组名到规则组定义的映射，跨插件冲突的组名已被剔除
        """
        return self._resolve(
            self._groups, subject="筛选规则组", hook="provides_filter_rule_groups"
        )

    def owners(self) -> Tuple[str, ...]:
        """列出当前有登记的全部实例键。

        :return: 实例键元组
        """
        with self._lock:
            return tuple(dict.fromkeys((*self._rules, *self._groups)))

    def claims(self) -> Tuple[FilterRuleClaim, ...]:
        """列出插件对各标识的声明及其裁决结果。

        冲突失效的标识同样交出：用户只在日志里见过一次告警，端点要能回答「这个标识
        为什么不生效、涉及哪些插件」。结果按种类与标识排序，与登记先后无关。

        :return: 按 (种类, 标识) 排序的声明元组
        """
        with self._lock:
            snapshots = (
                (RULE_KIND, dict(self._rules)),
                (RULE_GROUP_KIND, dict(self._groups)),
            )
        claims: List[FilterRuleClaim] = []
        for kind, table in snapshots:
            for identity, owners_by_plugin in self._collect_claimants(table).items():
                plugins = tuple(sorted(owners_by_plugin))
                claims.append(FilterRuleClaim(
                    kind=kind,
                    identity=identity,
                    plugins=plugins,
                    owners=tuple(owners_by_plugin[plugin] for plugin in plugins),
                    effective=len(plugins) == 1,
                ))
        return tuple(sorted(claims, key=lambda claim: (claim.kind, claim.identity)))

    def diagnose(self) -> List[Dict[str, Any]]:
        """输出只读的登记诊断信息。

        :return: 每条登记的标识种类、标识、声明方实例键与是否因跨插件冲突失效
        """
        return [
            {
                "kind": claim.kind,
                "identity": claim.identity,
                "owner": owner,
                "effective": claim.effective,
            }
            for claim in self.claims()
            for owner in claim.owners
        ]

    def _resolve(
        self, table: Dict[str, Dict[str, dict]], *, subject: str, hook: str
    ) -> Dict[str, dict]:
        """按跨插件冲突处置规则交出当前生效的登记内容。

        :param table: 实例键到「标识到定义」的映射
        :param subject: 标识在告警文案里的称呼
        :param hook: 声明钩子名，用于告警文案
        :return: 标识到定义的映射，被多个插件声明的标识已被剔除
        """
        with self._lock:
            snapshot = {owner: dict(items) for owner, items in table.items()}
        claimants = self._collect_claimants(snapshot)
        resolved: Dict[str, dict] = {}
        for identity, owners_by_plugin in claimants.items():
            if len(owners_by_plugin) > 1:
                self._warn_conflict(identity, owners_by_plugin, subject=subject, hook=hook)
                continue
            owner = next(iter(owners_by_plugin.values()))
            resolved[identity] = snapshot[owner][identity]
        return resolved

    @staticmethod
    def _collect_claimants(
        table: Dict[str, Dict[str, dict]]
    ) -> Dict[str, Dict[str, str]]:
        """按标识归拢声明方，同一插件的多个实例收敛为一个。

        同一插件的多个实例声明同一标识已由扩展级裁决在投影处消解，此处仍按同一
        规则收敛一次作为兜底：只读实例键本身，任何登记顺序都得到同一个结果。

        :param table: 实例键到「标识到定义」的映射
        :return: 标识到「插件标识到实例键」的映射
        """
        claimants: Dict[str, Dict[str, str]] = {}
        for owner, items in table.items():
            plugin_id = extension_id_of(owner)
            for identity in items:
                owners_by_plugin = claimants.setdefault(identity, {})
                current = owners_by_plugin.get(plugin_id)
                if current is None or instance_precedence(owner) < instance_precedence(current):
                    owners_by_plugin[plugin_id] = owner
        return claimants

    def _warn_conflict(
        self,
        identity: str,
        owners_by_plugin: Dict[str, str],
        *,
        subject: str,
        hook: str,
    ) -> None:
        """就一个标识被多个插件声明打一次提示。

        :param identity: 冲突的标识
        :param owners_by_plugin: 插件标识到实例键的映射
        :param subject: 标识在告警文案里的称呼
        :param hook: 声明钩子名
        :return: 无返回值
        """
        plugin_ids = tuple(sorted(owners_by_plugin))
        seen = (subject, identity, plugin_ids)
        with self._lock:
            if seen in self._conflicts_warned:
                return
            self._conflicts_warned.add(seen)
        self._logger.warning(
            f"{len(plugin_ids)} 个插件声明了同一个{subject} {identity}："
            f"{list(plugin_ids)}；{hook}() 声明的是数据而不是实现，宿主无从裁决"
            f"哪一份语义为准，按登记顺序取其一会让筛选行为随插件加载顺序变化，"
            f"因此该{subject}的全部插件声明一并失效"
            f"（内建同名定义仍然生效，用户自定义仍然优先）。"
            f"请让其中一方改用别的标识，或停用其中一个插件"
        )


plugin_filter_rule_registry = PluginFilterRuleRegistry()
