"""媒体数据源声明的方法表投影与 source 路由。

多来源契约由多个数据源共用同一个分发名，靠调用方传入的 ``source`` 区分来源。裸方法表
不带来源归属，宿主无从知道表里的实现服务哪个来源，让出只能由实现方自行判断——非本来源
必须返回 None，返回空列表会被判定为已认领并短路，把该契约下的其余来源一并拦掉。

`MediaSourceDeclaration` 把来源标识与实现放进同一条声明，来源归属因此对宿主可见：声明
里的多来源契约方法按声明的 ``media_source`` 包一层路由，调用带的来源不匹配时直接返回
None，实现不被触达。让出由宿主保证，不依赖实现方的纪律。

路由只作用于按 ``source`` 收窄的契约方法，判据取自模块契约登记表而非另列名单；
``media_exists`` 这类按 ``server``/``itemid`` 收窄的多来源能力不在其列，原样挂载。
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Mapping, Optional

from app.runtime.extensions.contract.declaration import (
    declaration_media_source_identity,
    declaration_media_source_methods,
)
from app.runtime.extensions.contract.module_method import get_multi_source_contract
from app.schemas.media import normalize_media_source

_ASYNC_PREFIX = "async_"
_SOURCE_NARROWING_KEY = "source"


def routes_by_source(method: str) -> bool:
    """
    判断方法名是否属于按 source 收窄的多来源契约

    ``async_`` 变体与同名同步方法共用一份契约登记，去前缀后再查表。

    :param method: 模块方法名
    :return: 该方法按 source 区分来源时为 True
    """
    base = method[len(_ASYNC_PREFIX):] if method.startswith(_ASYNC_PREFIX) else method
    contract = get_multi_source_contract(base)
    if contract is None:
        return False
    return any(key == _SOURCE_NARROWING_KEY for key, _description in contract.narrowing)


def normalized_media_source(media_source: Optional[str]) -> Optional[str]:
    """
    把来源标识归一为路由键

    :param media_source: 声明或调用给出的来源标识
    :return: 归一后的标识；无法归一时退回原始字符串，标识为空时为 None
    """
    if media_source is None or media_source == "":
        return None
    normalized = normalize_media_source(media_source)
    if normalized is not None:
        return normalized.value
    return str(media_source).strip() or None


def media_source_method_table(declarations: List[Any]) -> Dict[str, Any]:
    """
    把同一实例的媒体数据源声明合并为一张可分发的方法表

    按 source 收窄的契约方法合并成一个按来源路由的入口，同一实例声明多个数据源时
    各来源的同名实现因此互不覆盖；其余方法名原样挂载，同名后一条覆盖前一条。

    :param declarations: 已通过契约校验的媒体数据源声明列表
    :return: 方法名到可调用对象的映射
    """
    routed: Dict[str, Dict[str, Any]] = {}
    plain: Dict[str, Any] = {}
    for declaration in declarations:
        media_source, _name = declaration_media_source_identity(declaration)
        route_key = normalized_media_source(media_source)
        methods = declaration_media_source_methods(declaration)
        if not route_key or not isinstance(methods, Mapping):
            continue
        for method, func in methods.items():
            if routes_by_source(method):
                routed.setdefault(method, {})[route_key] = func
            else:
                plain[method] = func
    table: Dict[str, Any] = dict(plain)
    for method, targets in routed.items():
        table[method] = _source_routed(targets)
    return table


def _source_routed(targets: Mapping[str, Any]) -> Callable[..., Any]:
    """
    构造按调用方 source 选择实现的分发入口

    调用未带 source、或带的来源不在本表内时返回 None，即按弃权协议让出，交由分发
    继续询问下一个来源。目标中含协程函数时入口本身是协程函数，否则是同步函数——
    分发器按 `inspect.iscoroutinefunction` 决定是直接 await 还是丢进线程池，入口的
    形态必须与被包装的实现一致。

    :param targets: 归一后的来源标识到实现的映射
    :return: 按 source 路由的可调用对象
    """
    routes = dict(targets)

    def _resolve(kwargs: Dict[str, Any]) -> Optional[Callable[..., Any]]:
        """按调用参数里的 source 取本次应答的实现。"""
        route_key = normalized_media_source(kwargs.get(_SOURCE_NARROWING_KEY))
        return routes.get(route_key) if route_key else None

    if any(inspect.iscoroutinefunction(func) for func in routes.values()):
        async def _routed_async(*args: Any, **kwargs: Any) -> Any:
            """把调用路由到本来源的实现，非本来源让出。"""
            func = _resolve(kwargs)
            if func is None:
                return None
            result = func(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        return _routed_async

    def _routed(*args: Any, **kwargs: Any) -> Any:
        """把调用路由到本来源的实现，非本来源让出。"""
        func = _resolve(kwargs)
        return func(*args, **kwargs) if func is not None else None

    return _routed
