"""内建模块在清单里对自身身份的声明，以及按坐标取用这些声明的索引。

内建模块在形式上已是预装扩展——每个模块各有一份 `capability.toml`——但有两项身份此
前只能靠宿主写死或按缺省推定：这个模块承载的服务实例类型能配几份，以及这个模块服务
哪些媒体数据源。两项都是模块自己的事实，宿主无从推导，因此改由模块在清单里声明。

索引只读清单，不碰运行态：清单是磁盘上的数据，模块起没起来都读得到，因此按类型问
「能配几份」在模块尚未启动时同样有答案。清单读取有代价，故索引连同它所描述的那个发现
根一起缓存，根不变就复用；建索引的输入是声明快照，输出与声明的先后无关。

缺省即今天的行为：不写 ``service_instance`` 的模块取不到多份判定，调用方按多实例处理；
不写 ``media_sources`` 的模块交出空来源集合。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Tuple

from app.runtime.capabilities.model import CapabilitySpec
from app.runtime.extensions.lifecycle.host_module_adapter import (
    build_host_module_registry,
    declared_media_sources,
    host_module_root,
    service_instance_declaration,
)
from app.runtime.log import logger


@dataclass(frozen=True, slots=True)
class BuiltinDeclarationIndex:
    """内建模块身份声明的只读索引。

    :param multiplicity: 「能力标签加类型标识」到「能否配多份」的映射
    :param media_sources: 模块标识到其声明服务的媒体来源标识的映射
    """

    multiplicity: Mapping[Tuple[str, str], bool]
    media_sources: Mapping[str, Tuple[str, ...]]

    def multi_instance(self, capability: str, service_type: str) -> Optional[bool]:
        """回答指定服务实例类型能否被配置多份。

        :param capability: 能力标签
        :param service_type: 类型标识
        :return: 能否配多份；该类型未声明时为 None，由调用方按缺省处置
        """
        if not capability or not service_type:
            return None
        return self.multiplicity.get((capability, service_type))

    def all_media_sources(self) -> Tuple[str, ...]:
        """列出全部内建模块声明服务的媒体来源标识。

        :return: 去重后按标识升序排列的来源标识元组
        """
        return tuple(sorted({
            source
            for sources in self.media_sources.values()
            for source in sources
        }))


def build_declaration_index(
    specs: Iterable[CapabilitySpec],
) -> BuiltinDeclarationIndex:
    """从模块声明快照建立身份索引。

    同一坐标被多个模块声明在清单校验时即被拒，因此此处不再裁决归属；索引内容只由
    声明本身决定，与遍历先后无关。

    :param specs: 模块声明快照
    :return: 身份索引
    """
    multiplicity: dict[Tuple[str, str], bool] = {}
    media_sources: dict[str, Tuple[str, ...]] = {}
    for spec in specs:
        declaration = service_instance_declaration(spec)
        if declaration is not None:
            capability, service_type, multi_instance = declaration
            multiplicity[(capability, service_type)] = multi_instance
        sources = declared_media_sources(spec)
        if sources:
            media_sources[spec.id] = sources
    return BuiltinDeclarationIndex(
        multiplicity=MappingProxyType(multiplicity),
        media_sources=MappingProxyType(media_sources),
    )


_EMPTY_INDEX = BuiltinDeclarationIndex(
    multiplicity=MappingProxyType({}),
    media_sources=MappingProxyType({}),
)

_lock = threading.RLock()
# 缓存连同它描述的那个发现根一起记：索引是某一份清单根的投影，换了根就是另一份索引
_cache: Optional[tuple[Path, BuiltinDeclarationIndex]] = None


def declaration_index() -> BuiltinDeclarationIndex:
    """返回内建模块身份声明索引，首次取用时读取清单。

    清单读不出来时交出空索引而不向上抛：取用方问的是「这个类型能配几份」这类补充
    事实，答不上来时按缺省处置即可，不该让一次清单读取失败击穿正在服务的调用。

    :return: 身份索引
    """
    global _cache
    root = host_module_root()
    with _lock:
        if _cache is not None and _cache[0] == root:
            return _cache[1]
        try:
            index = build_declaration_index(build_host_module_registry().list_specs())
        except Exception as error:
            logger.error(f"【模块】读取内建模块身份声明失败，按未声明处理：{error}")
            index = _EMPTY_INDEX
        _cache = (root, index)
        return index


def reset_declaration_index() -> None:
    """丢弃已建立的索引，使下次取用重新读取清单。"""
    global _cache
    with _lock:
        _cache = None


def builtin_multi_instance(capability: str, service_type: str) -> Optional[bool]:
    """回答指定内建服务实例类型能否被配置多份。

    :param capability: 能力标签
    :param service_type: 类型标识
    :return: 能否配多份；该类型未由内建模块声明时为 None
    """
    return declaration_index().multi_instance(capability, service_type)


def builtin_media_sources() -> Tuple[str, ...]:
    """列出内建模块声明服务的全部媒体来源标识。

    :return: 去重后按标识升序排列的来源标识元组
    """
    return declaration_index().all_media_sources()


def builtin_module_media_sources() -> Mapping[str, Tuple[str, ...]]:
    """列出每个内建模块声明服务的媒体来源标识。

    :return: 模块标识到来源标识元组的映射，只含声明了来源的模块
    """
    return declaration_index().media_sources
