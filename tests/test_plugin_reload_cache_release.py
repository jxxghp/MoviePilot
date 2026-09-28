"""插件重载与路由移除后，旧插件代码不得被第三方进程级缓存留在内存中。"""

from __future__ import annotations

import gc
import sys
import types
import typing
import weakref
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.web.plugin.routes import FastAPIDynamicRouteRegistry
from app.runtime.extensions.plugin.loader import PluginLoader

_SILENT_LOG = SimpleNamespace(
    debug=lambda *_args: None,
    info=lambda *_args: None,
    warning=lambda *_args: None,
    error=lambda *_args: None,
)


def _install_plugin_module(name: str, source: str) -> types.ModuleType:
    """以插件运行时命名空间登记一个内存模块，模拟已导入的插件源码。"""
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, f"<{name}>", "exec"), module.__dict__)
    return module


def test_removed_plugin_route_releases_endpoint_from_fastapi_caches() -> None:
    """插件路由移除后，FastAPI 可调用分类缓存不再持有旧端点及其模块。"""
    module = _install_plugin_module(
        "app.plugins.routecachedemo",
        "async def health():\n    return {'ok': True}\n",
    )
    apis: list[dict] = [
        {
            "path": "/RouteCacheDemo/health",
            "endpoint": module.health,
            "methods": ["GET"],
            "allow_anonymous": True,
        }
    ]
    app = FastAPI()
    registry = FastAPIDynamicRouteRegistry(
        app=app,
        plugin_ids=lambda: ["RouteCacheDemo"],
        plugin_apis=lambda _plugin_id: apis,
        verify_token=lambda: None,
        verify_apikey=lambda: None,
        prefix="/api/v1/plugin",
        protected_routes=set(),
        log=_SILENT_LOG,
    )
    registry.update("RouteCacheDemo", "add")
    # 真实请求让 FastAPI 把端点写入分类缓存
    with TestClient(app) as client:
        assert client.get("/api/v1/plugin/RouteCacheDemo/health").json() == {"ok": True}

    endpoint_ref = weakref.ref(module.health)
    apis.clear()
    sys.modules.pop(module.__name__)
    del module
    registry.update("RouteCacheDemo", "remove")
    gc.collect()

    assert endpoint_ref() is None


def test_clear_modules_releases_plugin_classes_from_type_caches(tmp_path: Path) -> None:
    """清除插件模块缓存后，Pydantic 泛型缓存与 typing 缓存不再持有旧插件类。"""
    module = _install_plugin_module(
        "app.plugins.typecachedemo",
        "from typing import Optional\n"
        "from pydantic import BaseModel, RootModel\n"
        "class Payload(BaseModel):\n"
        "    title: str\n"
        "PayloadList = RootModel[list[Payload]]\n"
        "MaybePayload = Optional[Payload]\n",
    )
    # 对照：其他模块的参数化结果不应被清掉，类身份保持不变
    host_model = _install_plugin_module(
        "app.schemas.typecachehost",
        "from pydantic import BaseModel, RootModel\n"
        "class HostItem(BaseModel):\n"
        "    name: str\n",
    )
    from pydantic import RootModel

    host_list = RootModel[list[host_model.HostItem]]
    payload_ref = weakref.ref(module.Payload)
    loader = PluginLoader(
        plugins_root=tmp_path,
        import_preparer=lambda *_args, **_kwargs: None,
        import_scanner=lambda *_args, **_kwargs: None,
        log=_SILENT_LOG,
    )

    removed = loader.clear_modules("TypeCacheDemo")
    del module
    gc.collect()

    assert removed == ["app.plugins.typecachedemo"]
    assert payload_ref() is None
    assert RootModel[list[host_model.HostItem]] is host_list
    sys.modules.pop(host_model.__name__)


def test_clear_modules_without_matching_modules_keeps_type_caches(tmp_path: Path) -> None:
    """没有可清除的插件模块时不触碰全局类型缓存。"""
    cached = typing.Optional[int]
    loader = PluginLoader(
        plugins_root=tmp_path,
        import_preparer=lambda *_args, **_kwargs: None,
        import_scanner=lambda *_args, **_kwargs: None,
        log=_SILENT_LOG,
    )

    assert loader.clear_modules("NotLoadedPlugin") == []
    assert typing.Optional[int] is cached
