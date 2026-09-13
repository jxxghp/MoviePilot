"""插件实例日志等级覆盖缓存与上下文绑定测试。"""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
from datetime import datetime, timedelta, timezone
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
    normalize_log_expiry,
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
        "DemoPluginWork",
        "DEBUG",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    assert get_plugin_instance_log_level_override("DemoPluginWork") is None
    assert get_effective_plugin_instance_log_level("DemoPluginWork") == "INFO"


def test_unexpired_override_survives_read():
    """未过期覆盖读取后仍然生效，失效时间按同一时刻带时区返回。

    读回的是带时区的时间：不带时区会让调用方按 UTC 解读时整体偏移，表现为提交的
    失效时刻与读回来的对不上。因此按时刻比较，而不是直接相减两种不同的时间。
    """
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    set_plugin_instance_log_level("DemoPluginWork", "WARNING", expires_at=expires_at)

    override = get_plugin_instance_log_level_override("DemoPluginWork")

    assert override is not None
    level_name, returned_expiry = override
    assert level_name == "WARNING"
    assert returned_expiry is not None
    assert returned_expiry.tzinfo is not None
    assert abs(returned_expiry.timestamp() - expires_at.timestamp()) < 1


def test_normalize_log_expiry_reads_naive_input_as_utc():
    """不带时区的失效时间按 UTC 解读，而不是按进程所在时区。"""
    naive = datetime(2026, 1, 1, 12, 0, 0)

    normalized = normalize_log_expiry(naive)

    assert normalized == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert normalize_log_expiry(None) is None


def test_normalize_log_expiry_converts_aware_input_to_utc():
    """带时区的失效时间原样换算到 UTC，描述的仍是同一个时刻。"""
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))

    normalized = normalize_log_expiry(aware)

    assert normalized == datetime(2026, 1, 1, 4, 0, 0, tzinfo=timezone.utc)


def test_naive_expiry_round_trips_without_timezone_shift():
    """提交裸时间再读回来必须是同一时刻，不因进程时区整体偏移。"""
    naive = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2)
    set_plugin_instance_log_level("DemoPluginWork", "DEBUG", expires_at=naive)

    override = get_plugin_instance_log_level_override("DemoPluginWork")

    assert override is not None
    _level_name, returned_expiry = override
    assert returned_expiry == naive.replace(tzinfo=timezone.utc)


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


def test_wrap_for_plugin_instance_is_idempotent_for_the_same_instance():
    """同一实例重复包装返回原对象，避免每次刷新注册都叠一层包装。"""

    def _callback() -> None:
        """空回调，只用于观察包装结果。"""

    wrapped = wrap_for_plugin_instance(_callback, "DemoPluginWork")

    assert wrap_for_plugin_instance(wrapped, "DemoPluginWork") is wrapped
    assert wrap_for_plugin_instance(wrapped, "OtherInstance") is not wrapped


def test_rebinding_a_wrapped_callable_to_another_instance_switches_the_context():
    """同一个回调先为 A 包装再为 B 包装，执行时上下文必须是 B。

    插件的 `get_api`／`get_service` 可能每次都返回同一份缓存好的声明，多个分身
    依次注册就会把同一个回调对象接连包装；拿旧包装器当内层的话，内层会在外层
    绑定之后再把上下文改回 A，B 的日志就按 A 的覆盖等级过滤。
    """
    seen: list[str | None] = []

    def _callback() -> None:
        seen.append(current_plugin_instance_id())

    for_a = wrap_for_plugin_instance(_callback, "InstanceA")
    for_b = wrap_for_plugin_instance(for_a, "InstanceB")
    for_b()

    assert seen == ["InstanceB"]
    # 重绑包的是内层原始回调，包装链不随重绑次数变长
    assert for_b.__wrapped__ is _callback
    assert wrap_for_plugin_instance(for_b, "InstanceB") is for_b
    # 已经交出去的 A 包装器不受重绑影响，仍按 A 绑定
    for_a()
    assert seen == ["InstanceB", "InstanceA"]


def test_rebinding_a_wrapped_coroutine_to_another_instance_switches_the_context():
    """异步回调重绑到另一个实例后，等待期间的上下文必须是新实例。"""
    seen: list[str | None] = []

    async def _callback() -> None:
        seen.append(current_plugin_instance_id())

    for_a = wrap_for_plugin_instance(_callback, "InstanceA")
    for_b = wrap_for_plugin_instance(for_a, "InstanceB")
    assert inspect.iscoroutinefunction(for_b)
    asyncio.run(for_b())

    assert seen == ["InstanceB"]
    assert for_b.__wrapped__ is _callback


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


@pytest.fixture(name="console_output")
def fixture_console_output(monkeypatch, tmp_path):
    """把真实控制台日志器的输出接到内存缓冲区，供用例断言用户实际看到了什么。

    `fake_writer` 把 `_get_console_logger` 整个换成哑对象，覆盖到的只有文件那一路；
    实例覆盖能不能贯穿到控制台，只有拿到 handler 真正写出的文本才算验证。
    """
    logfile = Path("moviepilot.log")
    monkeypatch.delenv("MOVIEPILOT_DISABLE_CONSOLE_LOG", raising=False)
    monkeypatch.setattr(LoggerManager, "_loggers", {})
    monkeypatch.setattr(LoggerManager, "_writer", _CapturingLogWriter())
    monkeypatch.setattr(LoggerManager, "_log_path", tmp_path)
    buffer = io.StringIO()
    console = LoggerManager()._get_console_logger(logfile)
    for handler in console.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(buffer)
    yield buffer
    # 标准库日志器按名字全局共享，换过流的是同一个 handler 对象；不重建，后续用例
    # 的控制台日志都会写进本用例这个已经没人看的缓冲区
    LoggerManager._setup_console_logger(logfile)


def test_looser_instance_override_reaches_console_output(console_output):
    """全局 INFO、实例覆盖 DEBUG 时，DEBUG 日志必须真的出现在控制台。

    等级判定归 `LoggerManager.logger` 一处，输出端若还各自按全局等级再过滤一遍，
    用户设了 DEBUG 会看到文件里有、控制台没有，与「覆盖立即生效」的接口承诺不符。
    """
    set_plugin_instance_log_level("DemoPluginWork", "DEBUG")

    with bind_plugin_instance("DemoPluginWork"):
        logger.debug("控制台可见性探针")

    assert "控制台可见性探针" in console_output.getvalue()


def test_looser_override_does_not_loosen_a_sibling_instance_console_output(console_output):
    """一个实例把等级放宽，不得让另一个实例的 DEBUG 日志跟着冒出来。"""
    set_plugin_instance_log_level("DemoPluginWork", "DEBUG")

    with bind_plugin_instance("SiblingInstance"):
        logger.debug("兄弟实例不该出现")

    assert "兄弟实例不该出现" not in console_output.getvalue()


def test_looser_override_does_not_loosen_host_console_output(console_output):
    """一个实例把等级放宽，不得让宿主自身未绑定实例的 DEBUG 日志跟着冒出来。"""
    set_plugin_instance_log_level("DemoPluginWork", "DEBUG")

    logger.debug("宿主自身不该出现")

    assert "宿主自身不该出现" not in console_output.getvalue()


def test_stricter_instance_override_is_still_filtered_out_of_console_output(console_output):
    """实例覆盖比全局更严格时，全局本会放行的 INFO 日志同样不得出现在控制台。"""
    set_plugin_instance_log_level("DemoPluginWork", "ERROR")

    with bind_plugin_instance("DemoPluginWork"):
        logger.info("被更严格覆盖挡住")

    assert "被更严格覆盖挡住" not in console_output.getvalue()
