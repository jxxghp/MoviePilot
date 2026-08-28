"""SearchChain 对宿主调用方和官方插件的 ABI 回归测试。"""

from __future__ import annotations

import inspect
import pickle
from typing import Any, Callable

import pytest

import app.chain.search as search_package
from app.chain.search import SearchChain

PUBLIC_PATCH_POINTS = (
    "process",
    "search_by_id",
    "search_by_title",
    "async_process",
    "async_search_by_id",
    "async_search_by_title",
    "async_process_stream",
    "async_search_by_title_stream",
)
PUBLIC_METHOD_KINDS = {
    "process": "sync",
    "search_by_id": "sync",
    "search_by_title": "sync",
    "async_process": "coroutine",
    "async_search_by_id": "coroutine",
    "async_search_by_title": "coroutine",
    "async_process_stream": "asyncgen",
    "async_search_by_title_stream": "asyncgen",
}
LUNA_PRIVATE_PATCH_POINTS = (
    "_SearchChain__search_all_sites",
    "_SearchChain__async_search_all_sites",
    "_SearchChain__async_search_all_sites_stream",
)
INTERNAL_EXPORTS = (
    "SearchCacheOwner",
    "SearchMediaOwner",
    "SearchMusicOwner",
    "SearchPaginationOwner",
    "SearchPlanOwner",
    "SearchProviderOwner",
    "SearchRecommendOwner",
    "SearchResultOwner",
    "SearchSubtitleOwner",
    "SearchTitleOwner",
    "_SearchOwnerBase",
    "recommend_coordinator",
)
REQUIRED = "<required>"
EXPECTED_SIGNATURES = {
    "process": (
        ("self", REQUIRED),
        ("mediainfo", REQUIRED),
        ("keyword", None),
        ("no_exists", None),
        ("sites", None),
        ("rule_groups", None),
        ("area", "title"),
        ("custom_words", None),
        ("filter_params", None),
    ),
    "search_by_id": (
        ("self", REQUIRED),
        ("media_source", REQUIRED),
        ("media_id", REQUIRED),
        ("mtype", None),
        ("area", "title"),
        ("season", None),
        ("sites", None),
        ("cache_local", False),
        ("music_type", None),
    ),
    "search_by_title": (
        ("self", REQUIRED),
        ("title", REQUIRED),
        ("page", 0),
        ("sites", None),
        ("cache_local", False),
        ("mtype", None),
        ("rule_groups", None),
    ),
    "async_process": (
        ("self", REQUIRED),
        ("mediainfo", REQUIRED),
        ("keyword", None),
        ("no_exists", None),
        ("sites", None),
        ("rule_groups", None),
        ("area", "title"),
        ("custom_words", None),
        ("filter_params", None),
    ),
    "async_search_by_id": (
        ("self", REQUIRED),
        ("media_source", REQUIRED),
        ("media_id", REQUIRED),
        ("mtype", None),
        ("area", "title"),
        ("season", None),
        ("sites", None),
        ("cache_local", False),
        ("music_type", None),
    ),
    "async_search_by_title": (
        ("self", REQUIRED),
        ("title", REQUIRED),
        ("page", 0),
        ("sites", None),
        ("cache_local", False),
        ("mtype", None),
        ("rule_groups", None),
    ),
    "async_process_stream": (
        ("self", REQUIRED),
        ("mediainfo", REQUIRED),
        ("keyword", None),
        ("no_exists", None),
        ("sites", None),
        ("rule_groups", None),
        ("area", "title"),
        ("custom_words", None),
        ("filter_params", None),
    ),
    "async_search_by_title_stream": (
        ("self", REQUIRED),
        ("title", REQUIRED),
        ("page", 0),
        ("sites", None),
        ("cache_local", False),
        ("mtype", None),
        ("rule_groups", None),
    ),
    "_SearchChain__search_all_sites": (
        ("self", REQUIRED),
        ("keyword", REQUIRED),
        ("mediainfo", None),
        ("sites", None),
        ("page", 0),
        ("area", "title"),
        ("mtype", None),
    ),
    "_SearchChain__async_search_all_sites": (
        ("self", REQUIRED),
        ("keyword", REQUIRED),
        ("mediainfo", None),
        ("sites", None),
        ("page", 0),
        ("area", "title"),
        ("mtype", None),
    ),
    "_SearchChain__async_search_all_sites_stream": (
        ("self", REQUIRED),
        ("keyword", REQUIRED),
        ("mediainfo", None),
        ("sites", None),
        ("page", 0),
        ("area", "title"),
        ("mtype", None),
    ),
}


def _signature_shape(function: Callable[..., Any]) -> tuple[tuple[str, Any], ...]:
    """只锁定调用 ABI 所需的参数顺序和默认值，不耦合类型注解渲染。"""
    parameters = inspect.signature(function).parameters.values()
    return tuple(
        (
            parameter.name,
            REQUIRED if parameter.default is inspect.Parameter.empty else parameter.default,
        )
        for parameter in parameters
    )


def _replacement_for(function: Callable[..., Any]) -> Callable[..., Any]:
    """按原补丁点的同步、协程或异步生成器形态生成替代实现。"""
    if inspect.isasyncgenfunction(function):

        async def async_generator(*args, **kwargs):
            yield args, kwargs

        return async_generator
    if inspect.iscoroutinefunction(function):

        async def coroutine(*args, **kwargs):
            return args, kwargs

        return coroutine

    def synchronous(*args, **kwargs):
        return args, kwargs

    return synchronous


def test_search_chain_public_identity_and_pickle_contract():
    """类路径、限定名和最小实例的 pickle 查找路径保持历史稳定。"""
    assert SearchChain.__module__ == "app.chain.search"
    assert SearchChain.__qualname__ == "SearchChain"
    assert pickle.loads(pickle.dumps(SearchChain)) is SearchChain

    instance = object.__new__(SearchChain)
    restored = pickle.loads(pickle.dumps(instance))
    assert type(restored) is SearchChain
    assert vars(restored) == {}


@pytest.mark.parametrize("method_name", tuple(EXPECTED_SIGNATURES))
def test_search_chain_stable_method_signatures(method_name):
    """官方插件会覆写或直接调用的方法必须保持位置和默认参数兼容。"""
    signature = inspect.signature(getattr(SearchChain, method_name))
    assert all(parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for parameter in signature.parameters.values())
    assert _signature_shape(getattr(SearchChain, method_name)) == EXPECTED_SIGNATURES[method_name]


def test_luna_private_patch_points_keep_callable_shapes():
    """LunaTV 依赖的三个名称必须保留，其中流接口仍是真异步生成器。"""
    sync_method = getattr(SearchChain, LUNA_PRIVATE_PATCH_POINTS[0])
    async_method = getattr(SearchChain, LUNA_PRIVATE_PATCH_POINTS[1])
    stream_method = getattr(SearchChain, LUNA_PRIVATE_PATCH_POINTS[2])

    assert not inspect.iscoroutinefunction(sync_method)
    assert not inspect.isasyncgenfunction(sync_method)
    assert inspect.iscoroutinefunction(async_method)
    assert inspect.isasyncgenfunction(stream_method)


@pytest.mark.parametrize(("method_name", "expected_kind"), PUBLIC_METHOD_KINDS.items())
def test_hrblocker_public_patch_points_keep_callable_shapes(method_name, expected_kind):
    """公开补丁点的同步、协程和流式调用协议必须保持不变。"""
    method = getattr(SearchChain, method_name)
    actual_kind = (
        "asyncgen"
        if inspect.isasyncgenfunction(method)
        else "coroutine"
        if inspect.iscoroutinefunction(method)
        else "sync"
    )
    assert actual_kind == expected_kind


@pytest.mark.parametrize("method_name", LUNA_PRIVATE_PATCH_POINTS)
def test_luna_private_patch_points_can_be_replaced_and_restored(method_name):
    """模拟 LunaTV 的类级猴子补丁，验证拆分后仍可无损恢复。"""
    original = getattr(SearchChain, method_name)
    replacement = _replacement_for(original)
    try:
        setattr(SearchChain, method_name, replacement)
        assert getattr(SearchChain, method_name) is replacement
    finally:
        setattr(SearchChain, method_name, original)
    assert getattr(SearchChain, method_name) is original


@pytest.mark.parametrize("method_name", PUBLIC_PATCH_POINTS)
def test_hrblocker_public_patch_points_can_be_replaced_and_restored(method_name):
    """模拟 HRBlocker 的八个公开覆写点，防止门面绑定方式破坏补丁。"""
    original = getattr(SearchChain, method_name)
    replacement = _replacement_for(original)
    try:
        setattr(SearchChain, method_name, replacement)
        assert getattr(SearchChain, method_name) is replacement
    finally:
        setattr(SearchChain, method_name, original)
    assert getattr(SearchChain, method_name) is original


def test_remove_site_handler_keeps_public_identity():
    """事件装饰前固定公开身份，避免注册表暴露内部 facade 路径。"""
    assert SearchChain.remove_site.__module__ == "app.chain.search"
    assert SearchChain.remove_site.__qualname__ == "SearchChain.remove_site"


@pytest.mark.parametrize("symbol", INTERNAL_EXPORTS)
def test_search_package_root_does_not_export_internal_owners(symbol):
    """职责 owner 和协调器是宿主内部实现，不得扩大插件公开面。"""
    assert symbol not in search_package.__all__
    assert symbol not in vars(search_package)
    with pytest.raises(AttributeError):
        getattr(search_package, symbol)
