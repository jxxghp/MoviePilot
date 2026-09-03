"""插件实例日志等级覆盖缓存与上下文绑定测试。"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime import log as log_module
from app.runtime.log import (
    LoggerManager,
    bind_plugin_instance,
    clear_plugin_instance_log_level,
    current_plugin_instance_id,
    get_effective_plugin_instance_log_level,
    get_plugin_instance_log_level_override,
    logger,
    set_plugin_instance_log_level,
    wrap_for_plugin_instance,
)


class _CapturingLogWriter:
    """记录日志写入目标，避免测试访问真实文件系统。"""

    def __init__(self) -> None:
        """初始化空的调用记录列表。"""
        self.entries: list[tuple[str, str, Path]] = []

    def write_log(self, level: str, message: str, file_path: Path) -> None:
        """保存单条日志的级别、内容和目标路径。"""
        self.entries.append((level, message, file_path))

    @staticmethod
    def shutdown() -> bool:
        """测试写入器没有待释放资源。"""
        return True


@pytest.fixture(autouse=True)
def _isolate_plugin_log_state(monkeypatch):
    """快照并还原插件实例日志等级涉及的全部进程内全局状态，避免用例间互相污染。"""
    monkeypatch.setattr(log_module, "_plugin_level_overrides", {})
    monkeypatch.setattr(log_module.log_settings, "DEBUG", False)
    monkeypatch.setattr(log_module.log_settings, "LOG_LEVEL", "INFO")
    yield


@pytest.fixture(name="fake_writer")
def fixture_fake_writer(monkeypatch, tmp_path):
    """装配内存写入器，跳过真实文件 I/O。"""
    writer = _CapturingLogWriter()
    monkeypatch.setattr(LoggerManager, "_writer", writer)
    monkeypatch.setattr(LoggerManager, "_log_path", tmp_path)
    monkeypatch.setattr(
        LoggerManager,
        "_get_console_logger",
        classmethod(
            lambda _cls, _logfile: SimpleNamespace(
                info=lambda *_a, **_kw: None,
                debug=lambda *_a, **_kw: None,
                warning=lambda *_a, **_kw: None,
                error=lambda *_a, **_kw: None,
                critical=lambda *_a, **_kw: None,
            )
        ),
    )
    return writer


def test_set_and_get_effective_level_returns_override():
    """设置覆盖后按实例查询生效等级应返回覆盖值而非全局等级。"""
    set_plugin_instance_log_level("DemoPluginWork", "DEBUG")

    assert get_effective_plugin_instance_log_level("DemoPluginWork") == "DEBUG"
    assert get_effective_plugin_instance_log_level("OtherInstance") == "INFO"


def test_set_invalid_level_raises_value_error():
    """非受支持的等级名必须拒绝写入缓存。"""
    with pytest.raises(ValueError):
        set_plugin_instance_log_level("DemoPluginWork", "LOUD")

    assert get_plugin_instance_log_level_override("DemoPluginWork") is None


def test_clear_resets_to_global_level():
    """清除覆盖后立即回落全局等级。"""
    set_plugin_instance_log_level("DemoPluginWork", "ERROR")

    clear_plugin_instance_log_level("DemoPluginWork")

    assert get_plugin_instance_log_level_override("DemoPluginWork") is None
    assert get_effective_plugin_instance_log_level("DemoPluginWork") == "INFO"


def test_clear_is_idempotent_for_unset_instance():
    """清除一个从未设置过覆盖的实例不应报错。"""
    clear_plugin_instance_log_level("NeverConfigured")
    clear_plugin_instance_log_level("NeverConfigured")


def test_expired_override_evicts_on_read():
    """过期覆盖必须在读取时惰性判定并清理，而不是继续生效。"""
    set_plugin_instance_log_level(
        "DemoPluginWork", "DEBUG", expires_at=datetime.now() - timedelta(seconds=1)
    )

    assert get_plugin_instance_log_level_override("DemoPluginWork") is None
    assert get_effective_plugin_instance_log_level("DemoPluginWork") == "INFO"


def test_unexpired_override_survives_read():
    """未过期覆盖读取后仍然生效，且失效时间原样返回。"""
    expires_at = datetime.now() + timedelta(hours=1)
    set_plugin_instance_log_level("DemoPluginWork", "WARNING", expires_at=expires_at)

    override = get_plugin_instance_log_level_override("DemoPluginWork")

    assert override is not None
    level_name, returned_expiry = override
    assert level_name == "WARNING"
    assert returned_expiry is not None
    assert abs((returned_expiry - expires_at).total_seconds()) < 1


def test_override_does_not_leak_into_unrelated_instance_effective_level():
    """一个实例的覆盖不得影响另一个未设置覆盖实例的生效等级。"""
    set_plugin_instance_log_level("DemoPluginWork", "DEBUG")

    assert get_effective_plugin_instance_log_level("SiblingInstance") == "INFO"


def test_current_plugin_instance_id_defaults_to_none():
    """未绑定时读取当前实例上下文应为 None。"""
    assert current_plugin_instance_id() is None


def test_bind_plugin_instance_sets_and_resets_context():
    """绑定上下文管理器退出后必须恢复为未绑定状态，支持嵌套。"""
    assert current_plugin_instance_id() is None
    with bind_plugin_instance("Outer"):
        assert current_plugin_instance_id() == "Outer"
        with bind_plugin_instance("Inner"):
            assert current_plugin_instance_id() == "Inner"
        assert current_plugin_instance_id() == "Outer"
    assert current_plugin_instance_id() is None


def test_wrap_for_plugin_instance_binds_sync_callable():
    """同步回调包装后执行期间应能读到绑定的实例 ID。"""
    seen: list[str | None] = []

    def _callback() -> None:
        seen.append(current_plugin_instance_id())

    wrapped = wrap_for_plugin_instance(_callback, "DemoPluginWork")
    wrapped()

    assert seen == ["DemoPluginWork"]
    assert current_plugin_instance_id() is None


def test_wrap_for_plugin_instance_binds_async_callable():
    """异步回调包装后仍保持协程函数身份，且执行期间能读到绑定的实例 ID。"""
    seen: list[str | None] = []

    async def _callback() -> None:
        seen.append(current_plugin_instance_id())

    wrapped = wrap_for_plugin_instance(_callback, "DemoPluginWork")
    assert inspect.iscoroutinefunction(wrapped)
    asyncio.run(wrapped())

    assert seen == ["DemoPluginWork"]


def test_bound_instance_with_lower_override_emits_debug_log(fake_writer):
    """绑定实例设了更宽松的覆盖时，全局等级挡不住的 DEBUG 日志应放行。"""
    set_plugin_instance_log_level("DemoPluginWork", "DEBUG")

    with bind_plugin_instance("DemoPluginWork"):
        logger.debug("verbose diagnostic")

    assert any("verbose diagnostic" in message for _level, message, _path in fake_writer.entries)


def test_unbound_debug_log_is_dropped_by_global_level(fake_writer):
    """未绑定任何实例时，DEBUG 日志仍按全局 INFO 等级过滤丢弃。"""
    set_plugin_instance_log_level("DemoPluginWork", "DEBUG")

    logger.debug("verbose diagnostic without binding")

    assert fake_writer.entries == []


def test_bound_instance_with_stricter_override_drops_info_log(fake_writer):
    """绑定实例设了更严格的覆盖时，全局等级本会放行的 INFO 日志应被丢弃。"""
    set_plugin_instance_log_level("DemoPluginWork", "ERROR")

    with bind_plugin_instance("DemoPluginWork"):
        logger.info("routine progress")

    assert fake_writer.entries == []


def test_other_bound_instance_is_unaffected_by_sibling_override(fake_writer):
    """一个实例的覆盖不得影响另一个未设置覆盖实例的过滤结果。"""
    set_plugin_instance_log_level("DemoPluginWork", "ERROR")

    with bind_plugin_instance("SiblingInstance"):
        logger.info("sibling routine progress")

    assert any(
        "sibling routine progress" in message for _level, message, _path in fake_writer.entries
    )
