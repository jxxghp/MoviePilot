"""插件实例日志路由、按实例等级过滤与快速闸的行为契约测试。

日志模块的路由状态（等级覆盖缓存、日志目录解析器与缓存、写入器）都是进程内
共享的全局状态；本文件的自动生效 fixture 在每个用例前后完整快照/还原，避免
污染同一进程内的其余测试。
"""

import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.runtime import log as log_module


class _FakeWriter:
    """记录实际写入调用的内存日志写入器替身。"""

    def __init__(self):
        self.calls = []

    def write_log(self, level, message, file_path):
        self.calls.append((level, message, file_path))

    def shutdown(self):
        pass


class _FakeCode:
    def __init__(self, filename):
        self.co_filename = filename


class _FakeFrame:
    """`_get_caller` 栈回溯使用的最小帧替身，模拟指定文件路径的单帧调用链。"""

    def __init__(self, filename, back=None):
        self.f_code = _FakeCode(filename)
        self.f_back = back


@pytest.fixture(autouse=True)
def _isolate_plugin_log_state(monkeypatch):
    """快照并还原插件日志路由涉及的全部进程内全局状态。"""
    monkeypatch.setattr(log_module, "_plugin_level_overrides", {})
    monkeypatch.setattr(log_module, "_plugin_level_floor", log_module._current_global_log_level())
    monkeypatch.setattr(log_module, "_plugin_log_dir_resolver", None)
    monkeypatch.setattr(log_module, "_plugin_log_dir_cache", {})
    monkeypatch.setattr(log_module.LoggerManager, "_writer", None)
    monkeypatch.setattr(log_module.LoggerManager, "_log_path", None)
    monkeypatch.setattr(log_module.LoggerManager, "_pending_file_logs", log_module.deque(maxlen=1000))
    yield


@pytest.fixture(name="fake_writer")
def fixture_fake_writer(monkeypatch):
    """装配内存写入器并同步日志基准路径，跳过真实文件 I/O。"""
    writer = _FakeWriter()
    monkeypatch.setattr(log_module.LoggerManager, "_writer", writer)
    monkeypatch.setattr(log_module.LoggerManager, "_log_path", Path("/unused"))
    return writer


@pytest.fixture(name="instance_log_dir_resolver")
def fixture_instance_log_dir_resolver(tmp_path):
    """把插件实例日志目录解析器指向临时目录下的 <插件id>/<实例id>/logs。"""

    def resolver(plugin_id: str, instance_id: str) -> Path:
        return tmp_path / plugin_id / instance_id / "logs"

    log_module.configure_plugin_log_dir_resolver(resolver)
    return resolver


def _patch_stack_plugin_name(monkeypatch, plugin_name):
    """让 `_get_caller` 表现为栈回溯识别出给定插件名（实例未定位）。"""
    monkeypatch.setattr(
        log_module.LoggerManager,
        "_get_caller",
        staticmethod(lambda: ("demo.py", plugin_name)),
    )


# ---------------------------------------------------------------------------
# 1. 实例可辨识落到实例目录；不可辨识落到兜底目录且不丢失
# ---------------------------------------------------------------------------


def test_instance_identified_log_routes_to_instance_directory(
    fake_writer, instance_log_dir_resolver, monkeypatch
):
    """ContextVar 绑定了具体实例时，日志落到该实例的日志目录。"""
    _patch_stack_plugin_name(monkeypatch, None)

    with log_module.bind_plugin_instance("DemoPlugin", "second"):
        log_module.logger.info("hello from instance")

    assert len(fake_writer.calls) == 1
    _level, message, file_path = fake_writer.calls[0]
    assert "hello from instance" in message
    assert file_path == instance_log_dir_resolver("DemoPlugin", "second") / log_module.PLUGIN_LOG_FILENAME


def test_unattributed_plugin_log_routes_to_fallback_directory_without_loss(
    fake_writer, instance_log_dir_resolver, monkeypatch
):
    """只能栈回溯定位到插件、定位不到实例时，日志落到该插件的兜底目录，不丢失。"""
    _patch_stack_plugin_name(monkeypatch, "DemoPlugin")

    log_module.logger.warning("unattributed message")

    assert len(fake_writer.calls) == 1
    _level, message, file_path = fake_writer.calls[0]
    assert "unattributed message" in message
    expected_dir = instance_log_dir_resolver("DemoPlugin", log_module.UNATTRIBUTED_INSTANCE_ID)
    assert file_path == expected_dir / log_module.PLUGIN_LOG_FILENAME


def test_host_log_without_plugin_context_uses_default_log_file(fake_writer, monkeypatch):
    """既无 ContextVar 绑定也无法栈回溯到插件时，按宿主默认日志文件路由。"""
    _patch_stack_plugin_name(monkeypatch, None)

    log_module.logger.info("host message")

    assert len(fake_writer.calls) == 1
    _level, message, file_path = fake_writer.calls[0]
    assert file_path == Path("/unused") / log_module.LoggerManager._default_log_file


# ---------------------------------------------------------------------------
# 2. 插件自建线程的日志落到预期位置（覆盖不到时落在兜底目录）
# ---------------------------------------------------------------------------


def test_context_var_does_not_propagate_to_plugin_created_threads():
    """contextvars 只在同一协程/任务链内传播，插件自建的原生线程看不到宿主绑定。"""
    results = {}

    def worker():
        results["child_thread"] = log_module.LoggerManager._resolve_plugin_instance("DemoPlugin")

    with log_module.bind_plugin_instance("DemoPlugin", "second"):
        results["main_thread"] = log_module.LoggerManager._resolve_plugin_instance("DemoPlugin")
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

    assert results["main_thread"] == ("DemoPlugin", "second")
    # 子线程内看不到绑定，退回栈回溯识别出的插件名，实例定位不到
    assert results["child_thread"] == ("DemoPlugin", None)


def test_plugin_self_created_thread_log_falls_back_to_unattributed_directory(
    fake_writer, instance_log_dir_resolver, monkeypatch
):
    """插件自建线程内产生的日志覆盖不到实例绑定，落在该插件的兜底目录而不是丢失。"""
    _patch_stack_plugin_name(monkeypatch, "DemoPlugin")
    results = {}

    def worker():
        log_module.logger.info("message from plugin thread")
        results["calls"] = list(fake_writer.calls)

    with log_module.bind_plugin_instance("DemoPlugin", "second"):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

    assert len(results["calls"]) == 1
    _level, message, file_path = results["calls"][0]
    assert "message from plugin thread" in message
    expected_dir = instance_log_dir_resolver("DemoPlugin", log_module.UNATTRIBUTED_INSTANCE_ID)
    assert file_path == expected_dir / log_module.PLUGIN_LOG_FILENAME


# ---------------------------------------------------------------------------
# 3. 按实例等级过滤生效；等级过期后回落全局
# ---------------------------------------------------------------------------


def test_instance_level_override_filters_independently_of_global(
    fake_writer, instance_log_dir_resolver, monkeypatch
):
    """实例设为 ERROR 时，该实例的 INFO 日志被过滤；未覆盖的实例仍按全局 INFO 放行。"""
    _patch_stack_plugin_name(monkeypatch, None)
    log_module.set_plugin_instance_log_level("DemoPlugin", "quiet", "ERROR")

    with log_module.bind_plugin_instance("DemoPlugin", "quiet"):
        log_module.logger.info("should be filtered")
    with log_module.bind_plugin_instance("DemoPlugin", "default"):
        log_module.logger.info("should pass")

    messages = [message for _level, message, _path in fake_writer.calls]
    assert not any("should be filtered" in message for message in messages)
    assert any("should pass" in message for message in messages)


def test_instance_level_override_expires_and_falls_back_to_global(
    fake_writer, instance_log_dir_resolver, monkeypatch
):
    """覆盖过期后，实例的日志等级立即回落全局等级。"""
    _patch_stack_plugin_name(monkeypatch, None)
    log_module.set_plugin_instance_log_level(
        "DemoPlugin", "temp", "DEBUG", expires_at=datetime.now() + timedelta(seconds=-1)
    )

    assert log_module.get_plugin_instance_log_level_override("DemoPlugin", "temp") is None
    assert log_module.get_effective_plugin_instance_log_level("DemoPlugin", "temp") == "INFO"

    with log_module.bind_plugin_instance("DemoPlugin", "temp"):
        log_module.logger.debug("debug after expiry should be filtered")

    assert not fake_writer.calls


def test_clear_plugin_instance_log_level_falls_back_to_global(
    fake_writer, instance_log_dir_resolver, monkeypatch
):
    """清除覆盖后实例立即回落全局等级。"""
    _patch_stack_plugin_name(monkeypatch, None)
    log_module.set_plugin_instance_log_level("DemoPlugin", "temp", "DEBUG")
    log_module.clear_plugin_instance_log_level("DemoPlugin", "temp")

    assert log_module.get_plugin_instance_log_level_override("DemoPlugin", "temp") is None
    with log_module.bind_plugin_instance("DemoPlugin", "temp"):
        log_module.logger.debug("should be filtered after clear")

    assert not fake_writer.calls


def test_set_plugin_instance_log_level_rejects_invalid_level():
    """非法等级取值应拒绝写入缓存。"""
    with pytest.raises(ValueError):
        log_module.set_plugin_instance_log_level("DemoPlugin", "temp", "TRACE")


# ---------------------------------------------------------------------------
# 4. 快速闸：全局 INFO 且无实例覆盖更详细等级时，DEBUG 不触发栈回溯
# ---------------------------------------------------------------------------


def test_fast_gate_skips_stack_walk_when_no_instance_widens_level(monkeypatch):
    """全局 INFO 且无任何实例覆盖更详细等级时，被丢弃的 DEBUG 不应触发栈回溯。"""
    call_count = {"value": 0}
    original = log_module.LoggerManager._get_caller

    def counting_get_caller():
        call_count["value"] += 1
        return original()

    monkeypatch.setattr(log_module.LoggerManager, "_get_caller", staticmethod(counting_get_caller))

    log_module.logger.debug("dropped before stack walk")

    assert call_count["value"] == 0


def test_fast_gate_pays_stack_walk_cost_once_some_instance_widens_level(monkeypatch):
    """只要有实例开了更详细等级，快速闸阈值就会放宽，DEBUG 才会触发栈回溯。"""
    call_count = {"value": 0}
    original = log_module.LoggerManager._get_caller

    def counting_get_caller():
        call_count["value"] += 1
        return original()

    monkeypatch.setattr(log_module.LoggerManager, "_get_caller", staticmethod(counting_get_caller))
    log_module.set_plugin_instance_log_level("OtherPlugin", "verbose", "DEBUG")

    log_module.logger.debug("now passes the fast gate")

    assert call_count["value"] == 1


# ---------------------------------------------------------------------------
# 5. 宿主日志不再被误判为插件日志（app/plugins/__init__.py 自身产生的日志）
# ---------------------------------------------------------------------------


def test_get_caller_does_not_misattribute_plugins_package_file_as_plugin(monkeypatch):
    """`app/plugins/__init__.py` 自身产生的日志不应被误判成名为 __init__.py 的插件。"""
    frame = _FakeFrame("/srv/moviepilot/app/plugins/__init__.py")
    monkeypatch.setattr(sys, "_getframe", lambda depth: frame)

    _caller_name, plugin_name = log_module.LoggerManager._get_caller()

    assert plugin_name is None


def test_get_caller_still_attributes_genuine_plugin_subdirectory_file(monkeypatch):
    """插件子目录下的真实文件仍应被正确识别为该插件。"""
    frame = _FakeFrame("/srv/moviepilot/app/plugins/myplugin/__init__.py")
    monkeypatch.setattr(sys, "_getframe", lambda depth: frame)

    _caller_name, plugin_name = log_module.LoggerManager._get_caller()

    assert plugin_name == "myplugin"


def test_get_caller_attributes_nested_plugin_submodule_file(monkeypatch):
    """插件子目录更深层级的文件同样归属到该插件，而不是丢失识别。"""
    frame = _FakeFrame("/srv/moviepilot/app/plugins/myplugin/utils/helper.py")
    monkeypatch.setattr(sys, "_getframe", lambda depth: frame)

    _caller_name, plugin_name = log_module.LoggerManager._get_caller()

    assert plugin_name == "myplugin"


# ---------------------------------------------------------------------------
# 7. 等级缓存在配置变更后失效
# ---------------------------------------------------------------------------


def test_setting_new_level_invalidates_previous_effective_value():
    """重新设置等级后，查询立即反映新值，不残留旧的生效结果。"""
    log_module.set_plugin_instance_log_level("DemoPlugin", "temp", "ERROR")
    assert log_module.get_effective_plugin_instance_log_level("DemoPlugin", "temp") == "ERROR"

    log_module.set_plugin_instance_log_level("DemoPlugin", "temp", "DEBUG")
    assert log_module.get_effective_plugin_instance_log_level("DemoPlugin", "temp") == "DEBUG"


def test_configuring_new_log_dir_resolver_clears_cache(tmp_path):
    """重新装配日志目录解析器时清空既有缓存，不复用装配前解析出的目录。"""
    first_dir = tmp_path / "first"

    def first_resolver(_plugin_id, _instance_id):
        return first_dir

    log_module.configure_plugin_log_dir_resolver(first_resolver)
    assert log_module.get_plugin_instance_log_dir("DemoPlugin", "default") == first_dir

    second_dir = tmp_path / "second"

    def second_resolver(_plugin_id, _instance_id):
        return second_dir

    log_module.configure_plugin_log_dir_resolver(second_resolver)
    assert log_module.get_plugin_instance_log_dir("DemoPlugin", "default") == second_dir


def test_log_dir_resolver_result_is_cached_for_process_lifetime():
    """同一 (插件, 实例) 的目录解析结果被缓存，解析器不会被重复调用。"""
    call_count = {"value": 0}

    def resolver(plugin_id, instance_id):
        call_count["value"] += 1
        return Path(f"/tmp/{plugin_id}/{instance_id}")

    log_module.configure_plugin_log_dir_resolver(resolver)
    log_module.get_plugin_instance_log_dir("DemoPlugin", "default")
    log_module.get_plugin_instance_log_dir("DemoPlugin", "default")

    assert call_count["value"] == 1


def test_get_plugin_instance_log_dir_returns_none_when_resolver_not_configured():
    """解析器未装配时返回 None，路由端据此回落旧版扁平布局，而不是抛异常。"""
    assert log_module.get_plugin_instance_log_dir("DemoPlugin", "default") is None
