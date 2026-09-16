"""插件实例日志等级路由防遮蔽回归测试。

背景：`pluginloglevel.router` 与 `plugin.router` 在 `app/api/routers.py` 里共享同一个
`/plugin` 前缀，且 `plugin.router` 先注册、其中含有 `GET/PUT/DELETE /{plugin_id}` 这类
单段通配路由。FastAPI/Starlette 的路由分发按**注册顺序**取第一个完整匹配
（`Match.FULL`），不会按路径"更具体优先"排序，所以只要将来有人在 `plugin.py` 里新增
任何两段式（如 `/{plugin_id}/{action}`）或三段式路由，只要路径形状恰好覆盖了
`/loglevel/{plugin_id}` 或 `/loglevel/{plugin_id}/{instance_id}`，就会在注册顺序上抢在
`pluginloglevel.router` 之前被命中，把 `GET/PUT/DELETE /plugin/loglevel/...` 悄悄劫持到
`plugin.py` 里毫不相关的处理函数——三条 loglevel 路由本身的实现完全不受影响，出问题的
纯粹是路由表的注册顺序与路径形状。

现有端点测试（包括 `pluginloglevel` 自身的测试）都是直接 import 端点函数后同步调用，
完全绕开了路由匹配环节，因此这种遮蔽不会被现有测试察觉。单纯比较路径模板字符串
（例如断言两个 RouterSpec 的 prefix 都是 `/plugin`）同样测不出这个问题，因为它没有复现
「按注册顺序找第一个完整匹配」的真实分发算法——路径模板相同或不同都不能说明谁会先被
命中，唯一说得准的是实际跑一遍匹配算法。

本测试用与生产环境同款的组合根 `init_routers` 装配出真实 `FastAPI` 应用（不启动
lifespan、不连库、不发真实网络请求），为三条 loglevel URL 构造真实 ASGI scope。
FastAPI 较新版本的 `include_router` 是惰性组合，`app.routes` 中挂的是 `_IncludedRouter`
包装节点而非可直接匹配的叶子路由，需要通过 `effective_route_contexts()` 展平——这正是
`tests/test_router_aggregation.py::_v1_routes` 已经在用的既有做法，这里直接复用同一套
展平方式，避免另造一套。展平结果的顺序与真实 ASGI 分发时尝试候选路由的顺序完全一致，
在其上单趟遍历、调用每个候选自身的 `matches()`（内部走真实路径正则 + 方法集合判定），
即可精确复现「先找到第一个 FULL、否则记住第一个 PARTIAL」的分发语义，断言最终命中的
是 `pluginloglevel` 模块声明的端点函数，而不是 `plugin.py` 里的某个通配端点。
"""

from __future__ import annotations

from typing import Any, Callable

import pytest
from fastapi import FastAPI
from starlette.routing import Match

from app.api.endpoints import pluginloglevel
from app.runtime.config import settings
from app.startup.initializers.routers import init_routers


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """按生产环境同款组合根装配真实应用，仅用于路由匹配断言，不涉及运行期状态。"""
    application = FastAPI()
    init_routers(application)
    return application


def _flatten_route_candidates(app: FastAPI) -> list[Any]:
    """按真实注册/尝试顺序展平应用路由表，得到可直接 `.matches()` 的叶子候选列表。

    与 `tests/test_router_aggregation.py::_v1_routes` 使用同一套展平方式：FastAPI 的
    `include_router` 是惰性组合，`app.routes` 里挂的是 `_IncludedRouter` 包装节点，
    要用 FastAPI 自带的 `effective_route_contexts()` 递归展开才能拿到叶子路由；
    展开顺序与真实分发时逐层按各自 `router.routes` 声明顺序尝试候选的顺序一致。
    """
    candidates: list[Any] = []
    for route in app.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        if callable(effective_route_contexts):
            candidates.extend(effective_route_contexts())
        else:
            candidates.append(route)
    return candidates


def _build_scope(method: str, path: str) -> dict[str, Any]:
    """构造路由匹配所需的最小 ASGI scope。"""
    return {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "path_params": {},
    }


def _resolve_route(app: FastAPI, method: str, path: str) -> Any:
    """复现真实分发循环：按注册顺序找第一个完整匹配（`Match.FULL`）的候选路由。

    刻意不用字符串比较路径模板，而是交给每个候选自身的 `matches()`（对真实
    `APIRoute` 就是路径正则 + 方法集合判定），确保任何会影响真实分发结果的因素
    （正则、方法集合、参数转换器）都按生产逻辑复算，不会因为我们自己简化了判据
    而漏报。
    """
    scope = _build_scope(method, path)
    partial_match: Any = None
    for candidate in _flatten_route_candidates(app):
        match, _ = candidate.matches(scope)
        if match is Match.FULL:
            return candidate
        if match is Match.PARTIAL and partial_match is None:
            partial_match = candidate
    if partial_match is not None:
        raise AssertionError(
            f"{method} {path} 未命中任何完整匹配路由，仅命中路径匹配但方法不符的路由 "
            f"{partial_match.path!r}（methods={sorted(partial_match.methods or ())}）；"
            "请检查该方法是否确实已在对应路由器上注册。"
        )
    raise AssertionError(
        f"{method} {path} 未命中任何已注册路由，请检查 "
        "app/api/routers.py 的 API_V1_ROUTER_SPECS 是否遗漏了对应路由器。"
    )


def _assert_route_resolves(
    app: FastAPI, method: str, path: str, expected_endpoint: Callable[..., Any]
) -> None:
    """断言指定方法+路径按真实路由匹配落在预期端点函数上，否则报出抢先命中的路由。"""
    route = _resolve_route(app, method, path)
    assert route.endpoint is expected_endpoint, (
        f"{method} {path} 被路由 {route.path!r}（methods={sorted(route.methods or ())}）抢先命中，"
        f"实际解析到 {route.endpoint.__module__}.{route.endpoint.__qualname__}，"
        f"预期命中 {expected_endpoint.__module__}.{expected_endpoint.__qualname__}。"
        "很可能是 plugin.py 新增了路径形状覆盖 loglevel 路由的通配路由，"
        "且该路由在 app/api/routers.py 中注册顺序早于 pluginloglevel.router，因而抢先匹配。"
    )


def test_get_loglevel_overview_resolves_to_pluginloglevel_endpoint(app: FastAPI) -> None:
    """GET /plugin/loglevel/{plugin_id} 应命中 pluginloglevel.plugin_instance_log_levels。"""
    path = f"{settings.API_V1_STR}/plugin/loglevel/sample-plugin"
    _assert_route_resolves(app, "GET", path, pluginloglevel.plugin_instance_log_levels)


def test_put_loglevel_override_resolves_to_pluginloglevel_endpoint(app: FastAPI) -> None:
    """PUT /plugin/loglevel/{plugin_id}/{instance_id} 应命中 pluginloglevel.set_plugin_instance_log_level。"""
    path = f"{settings.API_V1_STR}/plugin/loglevel/sample-plugin/sample-instance"
    _assert_route_resolves(app, "PUT", path, pluginloglevel.set_plugin_instance_log_level)


def test_delete_loglevel_override_resolves_to_pluginloglevel_endpoint(app: FastAPI) -> None:
    """DELETE /plugin/loglevel/{plugin_id}/{instance_id} 应命中 pluginloglevel.clear_plugin_instance_log_level。"""
    path = f"{settings.API_V1_STR}/plugin/loglevel/sample-plugin/sample-instance"
    _assert_route_resolves(app, "DELETE", path, pluginloglevel.clear_plugin_instance_log_level)
