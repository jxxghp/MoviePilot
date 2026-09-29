"""可选子系统的按需导入合同测试。

Redis 缓存、AI 智能体对话记忆和音频容器格式只服务部分实例，宿主导入不得把它们的
第三方库常驻进未使用这些功能的进程。
"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

from app.startup.initializers import modules as modules_initializer


def _run_isolated(script: str) -> dict:
    """在全新解释器中执行导入探针，避免当前 pytest 模块缓存干扰。"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_factory_import_keeps_optional_subsystems_cold() -> None:
    """宿主应用导入不得加载 redis、langchain_core 或 mutagen 容器格式实现。"""
    result = _run_isolated(
        """
import json
import sys

import app.factory

forbidden = (
    "redis",
    "app.adapters.cache.redis",
    "langchain_core",
    "app.schemas.conversation",
    "mutagen.apev2",
    "mutagen.flac",
    "mutagen.id3",
    "mutagen.monkeysaudio",
    "mutagen.mp4",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
print(json.dumps({"loaded": loaded}))
"""
    )

    assert result == {"loaded": []}


def test_conversation_memory_legacy_paths_resolve_to_canonical_model() -> None:
    """旧导入路径仍解析到拆分后的同一个对话记忆模型。"""
    result = _run_isolated(
        """
import json

import app.schemas
from app.schemas.agent import ConversationMemory as legacy
from app.schemas.conversation import ConversationMemory as canonical

print(json.dumps({
    "legacy": legacy is canonical,
    "package": app.schemas.ConversationMemory is canonical,
}))
"""
    )

    assert result == {"legacy": True, "package": True}


def test_redis_shutdown_skips_when_adapter_not_imported(monkeypatch) -> None:
    """未导入 Redis 适配器时关闭步骤直接收敛，且不会为此导入它。"""
    monkeypatch.delitem(sys.modules, "app.adapters.cache.redis", raising=False)

    assert modules_initializer._close_existing_redis_helper("RedisHelper") is True
    assert modules_initializer._redis_reload_handler_providers() == {}
    assert "app.adapters.cache.redis" not in sys.modules


def test_config_reload_resolver_binds_redis_imported_after_registration(
    monkeypatch,
) -> None:
    """resolver 登记后才切换到 Redis 缓存时，仍能解析到当前 Redis owner。"""
    registered = {}
    monkeypatch.setattr(
        modules_initializer.EventManager,
        "register_handler_instance_resolver",
        lambda _self, name, resolver: registered.__setitem__(name, resolver),
    )
    monkeypatch.delitem(sys.modules, "app.adapters.cache.redis", raising=False)
    modules_initializer.configure_config_reload_event_handler_resolver()
    resolve = registered["config_reload"]

    class RedisHelper:
        """模拟晚于 resolver 导入的 Redis owner 类型。"""

        instance = None

        @classmethod
        def get_existing_instance(cls):
            """返回当前生命周期已创建的实例。"""
            return cls.instance

    class AsyncRedisHelper(RedisHelper):
        """模拟尚未创建实例的异步 Redis owner。"""

        instance = None

    RedisHelper.instance = RedisHelper()
    monkeypatch.setitem(
        sys.modules,
        "app.adapters.cache.redis",
        SimpleNamespace(RedisHelper=RedisHelper, AsyncRedisHelper=AsyncRedisHelper),
    )

    binding = resolve(RedisHelper)

    assert binding is not None
    assert binding.instance is RedisHelper.instance
