"""名称解析器注册表与解析管道。

登记由扩展生命周期驱动：扩展启动时把自己的解析环登记进来，停止时注销。执行顺序
不取登记顺序，而取用户在 `SystemConfigKey.MetaParserOrder` 里排的持久顺序——顺序
即语义，谁先跑决定谁的结果会被下游覆盖，这种选择不能由用户看不见的登记先后决定。
未出现在该配置里的解析器按声明的 priority 追加在末尾。

管道的控制权在宿主：每一环拿到当前累积结果，交回自己的贡献，宿主负责合并并记录
字段级溯源。解析器只能贡献，拿不到「继续或中断」的开关，因此一环失败只跳过这一环，
链条继续。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError

from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.log import logger
from app.schemas.metaparse import (
    BUILTIN_META_PARSER,
    PARSED_META_FIELDS,
    MetaFieldRevision,
    MetaParseOutcome,
    MetaParseRequest,
    MetaParseStatus,
    MetaParseTrace,
    MetaParserOrderEntry,
    MetaParserRun,
    ParsedMeta,
)


# 解析器标识中登记方实例键与声明标识的分隔符，标识本身不含该字符才能无损反解
PARSER_TOKEN_SEPARATOR = "#"

# 声明标识的合法取值，须能安全地出现在用户可编辑的顺序配置里
META_PARSER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 解析环的调用形状：接收一次解析请求，交回本环的贡献，None 表示不认领
MetaParserInvoke = Callable[[MetaParseRequest], Optional[ParsedMeta]]

# 顺序配置的读取形状，返回值即 systemconfig 中该键的原始内容
MetaParserOrderReader = Callable[[], Any]


def is_meta_parser_id(parser_id: Any) -> bool:
    """
    判断声明标识是否合法

    :param parser_id: 待判定的声明标识
    :return: 标识是非空字符串且只含字母、数字、点、下划线与短横时为 True
    """
    return isinstance(parser_id, str) and bool(META_PARSER_ID_RE.match(parser_id))


def meta_parser_token(parser_id: str, owner: Optional[str] = None) -> str:
    """
    组合解析器在宿主内的用户可见标识

    同一扩展的多个分身各自声明的同名解析器是各自成立的解析环，因此登记方实例键
    参与构成标识。

    :param parser_id: 声明标识
    :param owner: 登记方实例键，为空表示宿主内建解析器
    :return: 形如 ``AIMetaPlugin@alt#llm`` 的标识，无登记方时即声明标识本身
    """
    return f"{owner}{PARSER_TOKEN_SEPARATOR}{parser_id}" if owner else parser_id


def _empty_meta_parser_order() -> Any:
    """组合根尚未装配时返回空的顺序配置。"""
    return None


_order_reader: MetaParserOrderReader = _empty_meta_parser_order


def configure_meta_parser_order_reader(
    reader: MetaParserOrderReader,
) -> MetaParserOrderReader:
    """
    注入解析器顺序配置的读取能力

    :param reader: 返回顺序配置原始内容的可调用对象
    :return: 先前的 reader，供隔离环境恢复
    """
    global _order_reader
    previous = _order_reader
    _order_reader = reader
    return previous


@dataclass(frozen=True, slots=True)
class MetaParserEntry:
    """名称解析器在注册表中的一条登记。

    :param parser: 用户可见的解析器标识，登记键即此值
    :param name: 展示名称
    :param priority: 声明的默认顺序，数值越小越靠前，只在用户未排到该解析器时生效
    :param invoke: 解析环的调用入口
    :param distribution: 登记方的发行方式
    :param owner: 登记方实例键，宿主内建解析器为 None
    """

    parser: str
    name: str
    priority: int
    invoke: MetaParserInvoke
    distribution: ExtensionDistribution
    owner: Optional[str] = None


@dataclass(frozen=True, slots=True)
class MetaParserArbitration:
    """一条登记在顺序裁决中的位次归属与启停。

    :param entry: 被裁决的登记项
    :param enabled: 该环是否参与执行，停用的环仍占住自己的位次
    :param configured: 该环是否出现在用户排定的顺序里，为 False 表示按声明 priority 追加
    """

    entry: MetaParserEntry
    enabled: bool
    configured: bool


class MetaParserRegistry:
    """按解析器标识登记解析环，并按用户顺序串成管道。"""

    def __init__(self) -> None:
        """创建登记表。"""
        self._lock = threading.RLock()
        self._entries: Dict[str, MetaParserEntry] = {}

    def register(self,
                 parser_id: str,
                 invoke: MetaParserInvoke,
                 name: Optional[str] = None,
                 priority: int = 0,
                 owner: Optional[str] = None,
                 distribution: ExtensionDistribution = ExtensionDistribution.BUILTIN
                 ) -> Optional[str]:
        """
        登记一个解析环，同一标识重复登记以最新一次为准

        :param parser_id: 声明标识
        :param invoke: 解析环的调用入口
        :param name: 展示名称，为空时取解析器标识
        :param priority: 声明的默认顺序
        :param owner: 登记方实例键，为空表示宿主内建解析器
        :param distribution: 登记方的发行方式
        :return: 登记成功的解析器标识；标识非法或入口不可调用时为 None
        """
        source = owner or parser_id
        if not is_meta_parser_id(parser_id):
            logger.error(f"【识别】{source} 的解析器标识 {parser_id!r} 不合法，无法登记")
            return None
        if not callable(invoke):
            logger.error(f"【识别】{source} 的解析器 {parser_id} 没有可调用入口，无法登记")
            return None
        token = meta_parser_token(parser_id, owner)
        entry = MetaParserEntry(
            parser=token,
            name=(name or "").strip() or token,
            priority=int(priority or 0),
            invoke=invoke,
            distribution=distribution,
            owner=owner,
        )
        with self._lock:
            self._entries[token] = entry
        return token

    def unregister(self, parser: str, owner: Optional[str] = None) -> bool:
        """
        注销指定解析器标识的登记

        :param parser: 解析器标识
        :param owner: 注销方实例键，给出时只注销当前仍归属该登记方的条目
        :return: 该标识原本已登记且归属校验通过时为 True
        """
        if not parser:
            return False
        with self._lock:
            current = self._entries.get(parser)
            if current is None:
                return False
            if owner is not None and current.owner != owner:
                return False
            del self._entries[parser]
            return True

    def unregister_owner(self, owner: str) -> Tuple[str, ...]:
        """
        注销指定登记方当前仍生效的全部解析器登记

        :param owner: 登记方实例键
        :return: 被注销的解析器标识元组
        """
        with self._lock:
            owned = tuple(
                entry.parser for entry in self._entries.values() if entry.owner == owner
            )
            for parser in owned:
                self._entries.pop(parser, None)
            return owned

    def entries(self) -> Tuple[MetaParserEntry, ...]:
        """
        列出当前全部登记项

        :return: 登记项元组，按登记顺序排列
        """
        with self._lock:
            return tuple(self._entries.values())

    def meta_parser_count(self) -> int:
        """
        返回当前登记的解析环数量

        识别是热路径，领域层据此在一次转换都不做的前提下跳过整条管道。

        :return: 登记项数量
        """
        with self._lock:
            return len(self._entries)

    def arbitrated_entries(self) -> Tuple[MetaParserArbitration, ...]:
        """
        按用户顺序配置裁决全部登记项的先后与启停

        用户配置里排到的解析器按配置顺序排列；配置里没有的解析器按声明的 priority
        追加在末尾，priority 相同再按标识排序，因此结果与登记先后无关。停用的环留在
        自己的位次上并标记为停用，用户因而看得到它排在哪、也改得回来。

        :return: 按位次排列的裁决结果元组
        """
        entries = {entry.parser: entry for entry in self.entries()}
        ordered: List[MetaParserArbitration] = []
        seen: set[str] = set()
        for item in self.configured_order():
            entry = entries.get(item.parser)
            if entry is None or item.parser in seen:
                continue
            seen.add(item.parser)
            ordered.append(MetaParserArbitration(
                entry=entry, enabled=item.enabled, configured=True
            ))
        remaining = sorted(
            (entry for parser, entry in entries.items() if parser not in seen),
            key=lambda entry: (entry.priority, entry.parser),
        )
        return tuple([
            *ordered,
            *(
                MetaParserArbitration(entry=entry, enabled=True, configured=False)
                for entry in remaining
            ),
        ])

    def resolved_entries(self) -> Tuple[MetaParserEntry, ...]:
        """
        按用户顺序配置裁决本次执行的解析环及其先后

        与 `arbitrated_entries` 同一份裁决，只滤掉停用的环，两者不会因各算一遍而
        对不上。

        :return: 按执行顺序排列的登记项元组
        """
        return tuple(item.entry for item in self.arbitrated_entries() if item.enabled)

    @staticmethod
    def configured_order() -> Tuple[MetaParserOrderEntry, ...]:
        """
        读取并校验用户排定的解析器顺序

        单条配置畸形只跳过该条：顺序表是用户手写的持久数据，一条写错不该让整条
        管道退回默认顺序。

        :return: 按用户顺序排列的配置项元组
        """
        try:
            raw = _order_reader()
        except Exception as error:
            logger.error(f"【识别】读取解析器顺序配置出错：{str(error)}")
            return ()
        if not isinstance(raw, (list, tuple)):
            return ()
        configured: List[MetaParserOrderEntry] = []
        for item in raw:
            payload = {"parser": item} if isinstance(item, str) else item
            try:
                configured.append(MetaParserOrderEntry.model_validate(payload))
            except ValidationError as error:
                logger.warning(f"【识别】解析器顺序配置项 {item!r} 不合法，已跳过：{str(error)}")
        return tuple(configured)

    def run_meta_parsers(self, request: MetaParseRequest) -> MetaParseOutcome:
        """
        按用户顺序执行全部解析环，逐环合并结果并记录溯源

        一环抛异常或交回不合契约的结果只跳过该环，链条继续；解析器返回 None 即
        弃权，返回结果对象即认领，哪怕它一个字段都没改。

        :param request: 含标题、路径上下文与内建环已填结果的解析请求
        :return: 各环累积出的结果与字段级溯源
        """
        parsed = request.parsed
        trace = MetaParseTrace(revisions=_seed_revisions(parsed))
        for entry in self.resolved_entries():
            try:
                contribution = entry.invoke(request.model_copy(update={"parsed": parsed}))
            except Exception as error:
                logger.error(f"【识别】解析器 {entry.parser} 执行出错，已跳过：{str(error)}")
                trace.runs.append(MetaParserRun(
                    parser=entry.parser,
                    status=MetaParseStatus.FAILED,
                    error=str(error),
                ))
                continue
            if contribution is None:
                trace.runs.append(MetaParserRun(
                    parser=entry.parser, status=MetaParseStatus.ABSTAINED
                ))
                continue
            if not isinstance(contribution, ParsedMeta):
                message = f"{type(contribution).__name__} 不是 ParsedMeta"
                logger.error(f"【识别】解析器 {entry.parser} 交回的结果不合契约，已跳过：{message}")
                trace.runs.append(MetaParserRun(
                    parser=entry.parser, status=MetaParseStatus.FAILED, error=message
                ))
                continue
            parsed, written = _merge_contribution(parsed, contribution, entry.parser, trace)
            trace.runs.append(MetaParserRun(
                parser=entry.parser, status=MetaParseStatus.CLAIMED, fields=written
            ))
        return MetaParseOutcome(parsed=parsed, trace=trace)

    def diagnose(self) -> List[Dict[str, Any]]:
        """
        输出只读的登记诊断信息

        :return: 每个解析环的标识、名称、执行顺序、发行方式与登记方
        """
        return [
            {
                "parser": entry.parser,
                "name": entry.name,
                "order": index,
                "priority": entry.priority,
                "distribution": entry.distribution.value,
                "owner": entry.owner,
            }
            for index, entry in enumerate(self.resolved_entries())
        ]


def _seed_revisions(parsed: ParsedMeta) -> Dict[str, List[MetaFieldRevision]]:
    """
    把内建环已填的字段记为溯源的第一条写入

    :param parsed: 内建环交出的解析结果
    :return: 字段名到写入记录列表的映射，只含有取值的字段
    """
    return {
        field: [MetaFieldRevision(parser=BUILTIN_META_PARSER, value=value)]
        for field in PARSED_META_FIELDS
        if (value := getattr(parsed, field)) is not None
    }


def _merge_contribution(
    parsed: ParsedMeta,
    contribution: ParsedMeta,
    parser: str,
    trace: MetaParseTrace,
) -> Tuple[ParsedMeta, Tuple[str, ...]]:
    """
    把一环的贡献合并进累积结果，并登记本次写入

    只有真正改变取值的字段才算写入：与上游相同的取值既不是覆盖，也不必进溯源。

    :param parsed: 上游累积出的结果
    :param contribution: 本环交出的贡献
    :param parser: 本环的解析器标识
    :param trace: 本次解析的溯源，就地追加写入记录
    :return: (合并后的新结果, 本环实际写入的字段名元组)
    """
    updates: Dict[str, Any] = {}
    for field in PARSED_META_FIELDS:
        value = getattr(contribution, field)
        if value is None or value == getattr(parsed, field):
            continue
        updates[field] = value
    for field in contribution.clears:
        if field not in PARSED_META_FIELDS:
            logger.warning(f"【识别】解析器 {parser} 要求清空的字段 {field!r} 不在解析契约内，已忽略")
            continue
        if getattr(parsed, field) is None:
            continue
        updates[field] = None
    if not updates:
        return parsed, ()
    for field, value in updates.items():
        trace.revisions.setdefault(field, []).append(
            MetaFieldRevision(parser=parser, value=value)
        )
    return parsed.model_copy(update=updates), tuple(updates)


meta_parser_registry = MetaParserRegistry()
