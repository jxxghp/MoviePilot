"""名称解析管道的顺序与启停应用服务。

顺序即语义——谁先跑决定谁能覆盖谁的字段，因此这一族要回答的不是「登记了哪些环」，
而是「它们最终会按什么次序跑」。对外交出的列表因此是裁决后的生效顺序，而不是用户
排的那份原始配置：配置里没排到的环按声明 priority 追加在末尾，只看配置看不出它落在
哪儿。

内建识别在管道之外先跑完，其结果作为第一环的输入种子进入管道（见
`app.domain.meta.parsepipeline`），因此它恒定排在最前且无法停用。它仍然出现在列表里，
否则用户看不到扩展环究竟接在什么之后。
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from app.application.service_config import async_write_system_setting
from app.runtime.extensions.contract.instance import split_instance_key
from app.runtime.extensions.registry.meta_parser import (
    MetaParserArbitration,
    PARSER_TOKEN_SEPARATOR,
    meta_parser_registry,
)
from app.schemas.metaparse import (
    BUILTIN_META_PARSER,
    MetaParserOrderEntry,
    MetaParserPipeline,
    MetaParserRing,
)
from app.schemas.types import SystemConfigKey

# 内建解析环的展示名称
BUILTIN_META_PARSER_NAME = "内建识别"


class MetaParserPipelineService:
    """交出名称解析管道的生效顺序，并持久化用户排定的顺序与启停。"""

    def __init__(self, registry: Any = None) -> None:
        """
        绑定名称解析器注册表

        :param registry: 解析器注册表，为空时取宿主全局注册表
        """
        self._registry = registry or meta_parser_registry

    def list_pipeline(self) -> MetaParserPipeline:
        """
        列出全部解析环的最终生效顺序

        :return: 按生效顺序排列的解析环，内建环恒为第一条
        """
        rings = [self._builtin_ring()]
        for arbitration in self._registry.arbitrated_entries():
            rings.append(self._ring_of(arbitration, order=len(rings)))
        return MetaParserPipeline(rings=rings)

    async def save_order(
        self, order: Sequence[MetaParserOrderEntry]
    ) -> MetaParserPipeline:
        """
        保存用户排定的解析环顺序与启停

        当前未登记但用户此前排过的环留在原位次上：插件停用期间改顺序不该让它复跑时
        跳到末尾。

        :param order: 按目标顺序排列的顺序项
        :return: 保存后重新裁决出的生效顺序
        :raises ValueError: 顺序项标识为空、重复，或试图排定内建环
        """
        entries = self._validated_order(order)
        await self._persist(self._retaining_unregistered(entries))
        return self.list_pipeline()

    async def set_enabled(self, parser: str, enabled: bool) -> MetaParserPipeline:
        """
        单独启停一个解析环

        启停连同当前可见的位次一并落盘：只写这一条会让原本按 priority 追加的环失去
        自己的位次，用户改的是启停却看到顺序变了。

        :param parser: 解析环标识
        :param enabled: 目标启停状态
        :return: 保存后重新裁决出的生效顺序
        :raises ValueError: 试图启停内建环
        :raises LookupError: 该标识当前没有登记
        """
        if parser == BUILTIN_META_PARSER:
            raise ValueError("内建识别环由宿主固定执行，无法启停")
        arbitrations = self._registry.arbitrated_entries()
        if not any(item.entry.parser == parser for item in arbitrations):
            raise LookupError(f"名称解析环 {parser} 未登记")
        entries = [
            MetaParserOrderEntry(
                parser=item.entry.parser,
                enabled=enabled if item.entry.parser == parser else item.enabled,
            )
            for item in arbitrations
        ]
        await self._persist(self._retaining_unregistered(entries))
        return self.list_pipeline()

    @staticmethod
    def _builtin_ring() -> MetaParserRing:
        """
        构造内建解析环的呈现

        :return: 位次固定在最前、不可停用的解析环
        """
        return MetaParserRing(
            parser=BUILTIN_META_PARSER,
            parser_id=BUILTIN_META_PARSER,
            name=BUILTIN_META_PARSER_NAME,
            owner=None,
            extension_id=None,
            instance_id=None,
            priority=0,
            enabled=True,
            order=0,
            configured=False,
            distribution="builtin",
            pinned=True,
        )

    @staticmethod
    def _ring_of(arbitration: MetaParserArbitration, order: int) -> MetaParserRing:
        """
        把一条裁决结果拆解为用户看得懂的解析环呈现

        :param arbitration: 裁决结果
        :param order: 该环在生效顺序中的位次
        :return: 标识已按登记方与声明标识拆开的解析环
        """
        entry = arbitration.entry
        owner = entry.owner
        extension_id, instance_id = (
            split_instance_key(owner) if owner else (None, None)
        )
        return MetaParserRing(
            parser=entry.parser,
            parser_id=_declared_parser_id(entry.parser, owner),
            name=entry.name,
            owner=owner,
            extension_id=extension_id,
            instance_id=instance_id,
            priority=entry.priority,
            enabled=arbitration.enabled,
            order=order,
            configured=arbitration.configured,
            distribution=entry.distribution.value,
            pinned=False,
        )

    @staticmethod
    def _validated_order(
        order: Sequence[MetaParserOrderEntry],
    ) -> List[MetaParserOrderEntry]:
        """
        校验用户提交的顺序项

        :param order: 待校验的顺序项
        :return: 校验通过的顺序项列表
        :raises ValueError: 顺序项标识为空、重复，或试图排定内建环
        """
        seen: set[str] = set()
        validated: List[MetaParserOrderEntry] = []
        for item in order:
            parser = (item.parser or "").strip()
            if not parser:
                raise ValueError("名称解析环标识不能为空")
            if parser == BUILTIN_META_PARSER:
                raise ValueError("内建识别环由宿主固定执行，无法排定顺序")
            if parser in seen:
                raise ValueError(f"名称解析环 {parser} 在顺序里出现了多次")
            seen.add(parser)
            validated.append(MetaParserOrderEntry(parser=parser, enabled=item.enabled))
        return validated

    def _retaining_unregistered(
        self, entries: Sequence[MetaParserOrderEntry]
    ) -> List[MetaParserOrderEntry]:
        """
        把当前未登记但此前排过的环按原位次并回顺序里

        :param entries: 本次提交的顺序项
        :return: 并入历史位次后的顺序项列表
        """
        registered = {item.entry.parser for item in self._registry.arbitrated_entries()}
        submitted = {item.parser for item in entries}
        merged = list(entries)
        for index, item in enumerate(self._registry.configured_order()):
            if item.parser in submitted or item.parser in registered:
                continue
            merged.insert(min(index, len(merged)), item)
            submitted.add(item.parser)
        return merged

    @staticmethod
    async def _persist(entries: Sequence[MetaParserOrderEntry]) -> None:
        """
        把顺序项写入系统配置

        :param entries: 待落盘的顺序项
        :return: 无返回值
        """
        await async_write_system_setting(
            SystemConfigKey.MetaParserOrder,
            [item.model_dump() for item in entries],
        )


def _declared_parser_id(parser: str, owner: Optional[str]) -> str:
    """
    从解析环标识中取出扩展声明的那一段

    按登记方实例键的长度切分而不是找分隔符：实例标识允许用户自填，其中可能含有
    分隔符本身，按字符找会切错位置。

    :param parser: 解析环标识
    :param owner: 登记方实例键，为空表示宿主内建解析器
    :return: 声明标识
    """
    prefix = f"{owner}{PARSER_TOKEN_SEPARATOR}"
    if owner and parser.startswith(prefix):
        return parser[len(prefix):]
    return parser


__all__ = [
    "BUILTIN_META_PARSER_NAME",
    "MetaParserPipelineService",
]
