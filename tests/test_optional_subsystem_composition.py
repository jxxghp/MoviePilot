"""可选子系统按配置装配的合同测试。

Redis 缓存只在启动时选用 Redis 后由缓存组合根接入，智能体对话记忆模型归属智能体模块；
未使用这些功能的实例不得常驻 redis 客户端库或 langchain_core。
"""

from __future__ import annotations

import json
import subprocess
import sys

from app.runtime import cache as runtime_cache
from app.runtime.config import settings
from app.startup.composition import cache as cache_composition


def _run_isolated(script: str) -> dict:
    """在全新解释器中执行导入探针，先装测试站点垫片并隔离配置目录。"""
    prelude = (
        "from app.testing.bootstrap import ensure_sites_stub, isolate_config_dir\n"
        "isolate_config_dir()\n"
        "ensure_sites_stub()\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", prelude + script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_file_cache_startup_does_not_load_redis_or_langchain() -> None:
    """文件缓存实例导入宿主应用并完成缓存装配后，不加载 redis 与 langchain_core。"""
    result = _run_isolated(
        """
import json
import sys

import app.factory
from app.startup.composition.cache import get_cache_composition

forbidden = ("redis", "app.adapters.cache.redis", "langchain_core", "app.agent.memory")
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
print(json.dumps({"loaded": loaded, "redis_enabled": get_cache_composition().redis_enabled}))
"""
    )

    assert result == {"loaded": [], "redis_enabled": False}


def test_redis_cache_composition_registers_owners_and_backends(
    monkeypatch,
    compose_cache_backend,
) -> None:
    """启动时选用 Redis 才接入 Redis 后端与连接 owner。"""
    from app.adapters.cache.redis import AsyncRedisHelper, RedisBackend, RedisHelper

    monkeypatch.setattr(RedisHelper, "_connect", lambda _self: None)

    compose_cache_backend("redis")
    composition = cache_composition.get_cache_composition()

    assert composition.redis_enabled is True
    assert composition.redis_owners == (RedisHelper, AsyncRedisHelper)
    assert isinstance(runtime_cache.FileCache(), RedisBackend)
    assert runtime_cache.probe_redis_cache() is True


def test_redis_setting_after_local_startup_waits_for_restart(
    compose_cache_backend,
) -> None:
    """启动时未接入 Redis，运行中改成 redis 继续使用本地缓存且不探测连接。"""
    compose_cache_backend("cachetools")

    settings.CACHE_BACKEND_TYPE = "redis"

    assert cache_composition.get_cache_composition().redis_owners == ()
    assert not runtime_cache.FileCache().is_redis()
    assert runtime_cache.probe_redis_cache() is None


def test_redis_module_reports_restart_when_redis_not_composed(
    compose_cache_backend,
) -> None:
    """Redis 模块健康检查不代为建连，未接入时提示重启生效。"""
    from app.modules.redis import RedisModule

    compose_cache_backend("cachetools")
    settings.CACHE_BACKEND_TYPE = "redis"

    assert RedisModule().test() == (False, "缓存类型已改为Redis，重启后生效")


def test_legacy_redis_backend_and_conversation_memory_paths_stay_compatible() -> None:
    """Redis 后端与对话记忆模型的旧导入路径惰性解析到同一个对象。"""
    result = _run_isolated(
        """
import json

import app.schemas
import app.sdk.cache
from app.adapters.cache.backends import RedisBackend as backends_redis
from app.adapters.cache.redis import RedisBackend
from app.agent.memory import ConversationMemory
from app.schemas.agent import ConversationMemory as legacy_memory

print(json.dumps({
    "sdk": app.sdk.cache.RedisBackend is RedisBackend,
    "backends": backends_redis is RedisBackend,
    "schema_module": legacy_memory is ConversationMemory,
    "schema_package": app.schemas.ConversationMemory is ConversationMemory,
}))
"""
    )

    assert result == {
        "sdk": True,
        "backends": True,
        "schema_module": True,
        "schema_package": True,
    }
