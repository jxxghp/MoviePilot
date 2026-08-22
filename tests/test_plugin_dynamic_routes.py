"""插件多实例 API 动态路由隔离的行为契约测试。

覆盖两个实例各自的路由都能注册与访问、按实例键移除时不误删兄弟实例的路由、
按插件标识移除时命中全部实例的路由，以及默认实例的对外 URL 与单实例场景
完全一致（默认实例的实例键退化为裸插件标识）。
"""

from types import SimpleNamespace

from fastapi import FastAPI

from app.adapters.web.plugin.routes import FastAPIDynamicRouteRegistry
from app.runtime.extensions.contract.instance import matches_extension

_PREFIX = "/api/v1/plugin"
_INSTANCE_KEYS = ["DemoPlugin", "DemoPlugin@second"]


def _noop_endpoint():
    """占位路由处理函数。"""
    return {"ok": True}


def _api(path: str) -> dict:
    """构造插件声明的一条最小 API 项，匿名放行以跳过认证依赖装配。"""
    return {
        "path": path,
        "endpoint": _noop_endpoint,
        "methods": ["GET"],
        "auth": "apikey",
        "allow_anonymous": True,
    }


def _silent_logger() -> SimpleNamespace:
    """构造吞掉全部日志调用的替身。"""
    return SimpleNamespace(debug=lambda *_a, **_k: None, error=lambda *_a, **_k: None)


def _build_registry(
    app: FastAPI, instance_keys: list[str] = _INSTANCE_KEYS
) -> FastAPIDynamicRouteRegistry:
    """构造绑定固定插件实例目录的动态路由适配器。

    :param app: 目标 FastAPI 应用
    :param instance_keys: 参与投影的实例键清单，模拟 PluginProjection.apis()
        按 matches_extension 过滤后的产出
    """

    def plugin_ids() -> list[str]:
        """模拟 get_running_plugin_ids()：按插件标识去重。"""
        seen: list[str] = []
        for key in instance_keys:
            bare = key.split("@", 1)[0]
            if bare not in seen:
                seen.append(bare)
        return seen

    def plugin_apis(pid: str) -> list[dict]:
        """模拟按 pid 过滤后的插件 API 声明，路径已带实例键前缀。"""
        return [
            _api(f"/{key}/status")
            for key in instance_keys
            if matches_extension(key, pid)
        ]

    return FastAPIDynamicRouteRegistry(
        app=app,
        plugin_ids=plugin_ids,
        plugin_apis=plugin_apis,
        verify_token=lambda: None,
        verify_apikey=lambda: None,
        prefix=_PREFIX,
        protected_routes=set(),
        log=_silent_logger(),
        route_matches=matches_extension,
    )


def _plugin_route_paths(app: FastAPI) -> set:
    """返回应用当前登记的插件动态路由路径集合。"""
    return {
        route.path for route in app.routes if route.path.startswith(f"{_PREFIX}/")
    }


def test_update_registers_routes_for_every_instance():
    """注册全部插件时，两个实例各自的路由都被登记且可区分。"""
    app = FastAPI()
    registry = _build_registry(app)

    registry.update(None, "add")

    assert _plugin_route_paths(app) == {
        f"{_PREFIX}/DemoPlugin/status",
        f"{_PREFIX}/DemoPlugin@second/status",
    }


def test_remove_one_instance_keeps_sibling_route():
    """按实例键移除时只命中该实例，不误删兄弟实例的路由。"""
    app = FastAPI()
    registry = _build_registry(app)
    registry.update(None, "add")

    removed = registry.remove("DemoPlugin@second")

    assert removed is True
    assert _plugin_route_paths(app) == {f"{_PREFIX}/DemoPlugin/status"}


def test_remove_whole_plugin_removes_every_instance_route():
    """按插件标识移除时命中该插件的全部实例，不留下分身路由。"""
    app = FastAPI()
    registry = _build_registry(app)
    registry.update(None, "add")

    removed = registry.remove("DemoPlugin")

    assert removed is True
    assert _plugin_route_paths(app) == set()


def test_default_instance_route_matches_single_instance_url():
    """默认实例的对外 URL 与不区分实例时完全一致。"""
    app = FastAPI()
    registry = _build_registry(app, instance_keys=["DemoPlugin"])

    registry.update(None, "add")

    assert _plugin_route_paths(app) == {f"{_PREFIX}/DemoPlugin/status"}
