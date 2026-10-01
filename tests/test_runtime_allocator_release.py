"""分配器空闲页归还：按进程实际加载的分配器选择 jemalloc purge 或 glibc malloc_trim，以及线程缓存刷新。"""

from __future__ import annotations

import ctypes

import pytest

from app.runtime import gc as runtime_gc
from app.scheduler.maintenance import SchedulerMaintenanceOwner


class _FakeSymbol:
    """模拟 ctypes 导出函数，记录调用参数与签名设置。"""

    def __init__(self, result: int = 0):
        self.result = result
        self.calls: list[tuple] = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class _FakeLib:
    """只暴露给定符号的伪 libc，缺失符号与真实 ctypes 一样返回 AttributeError。"""

    def __init__(self, **symbols):
        self._symbols = symbols

    def __getattr__(self, name):
        try:
            return self._symbols[name]
        except KeyError:
            raise AttributeError(name) from None


@pytest.fixture(autouse=True)
def _fresh_symbol_resolution():
    """符号解析按进程缓存；每个用例换用不同的伪 libc，前后都清空缓存。"""
    runtime_gc._process_libc.cache_clear()
    runtime_gc._jemalloc_mallctl.cache_clear()
    yield
    runtime_gc._process_libc.cache_clear()
    runtime_gc._jemalloc_mallctl.cache_clear()


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(runtime_gc.sys, "platform", "linux")


def _use_lib(monkeypatch, lib) -> None:
    monkeypatch.setattr(runtime_gc.ctypes, "CDLL", lambda _name: lib)


def test_jemalloc_purges_all_arenas(linux, monkeypatch):
    mallctl = _FakeSymbol()
    trim = _FakeSymbol()
    _use_lib(monkeypatch, _FakeLib(mallctl=mallctl, malloc_trim=trim))

    assert runtime_gc.release_allocator_memory() == "jemalloc"
    assert mallctl.calls == [(b"arena.4096.purge", None, None, None, 0)]
    assert mallctl.restype is ctypes.c_int
    # jemalloc 生效时不再调用 glibc 接口
    assert trim.calls == []


def test_jemalloc_failure_does_not_fall_back_to_glibc(linux, monkeypatch):
    mallctl = _FakeSymbol(result=22)
    trim = _FakeSymbol()
    _use_lib(monkeypatch, _FakeLib(mallctl=mallctl, malloc_trim=trim))

    assert runtime_gc.release_allocator_memory() is None
    assert trim.calls == []


def test_glibc_trims_when_jemalloc_absent(linux, monkeypatch):
    trim = _FakeSymbol(result=1)
    _use_lib(monkeypatch, _FakeLib(malloc_trim=trim))

    assert runtime_gc.release_allocator_memory() == "glibc"
    assert trim.calls == [(0,)]


def test_unknown_allocator_is_a_no_op(linux, monkeypatch):
    _use_lib(monkeypatch, _FakeLib())

    assert runtime_gc.release_allocator_memory() is None


def test_non_linux_platform_skips_native_calls(monkeypatch):
    monkeypatch.setattr(runtime_gc.sys, "platform", "darwin")

    def unexpected(_name):
        raise AssertionError("非 Linux 平台不应加载 C 库")

    monkeypatch.setattr(runtime_gc.ctypes, "CDLL", unexpected)

    assert runtime_gc.release_allocator_memory() is None


def test_full_gc_releases_allocator_after_collect(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr("app.scheduler.maintenance.gc.collect", lambda: order.append("collect") or 3)
    monkeypatch.setattr(
        "app.scheduler.maintenance.release_allocator_memory", lambda: order.append("release") or "jemalloc"
    )
    monkeypatch.setattr("app.scheduler.maintenance.get_memory_usage", lambda: 100.0)

    SchedulerMaintenanceOwner.full_gc()

    assert order == ["collect", "release"]


def test_thread_cache_flush_uses_jemalloc(linux, monkeypatch):
    mallctl = _FakeSymbol()
    _use_lib(monkeypatch, _FakeLib(mallctl=mallctl))

    assert runtime_gc.flush_thread_allocator_cache() is True
    assert mallctl.calls == [(b"thread.tcache.flush", None, None, None, 0)]


def test_thread_cache_flush_resolves_symbols_once(linux, monkeypatch):
    mallctl = _FakeSymbol()
    loads: list[str] = []

    def load(_name):
        loads.append("load")
        return _FakeLib(mallctl=mallctl)

    monkeypatch.setattr(runtime_gc.ctypes, "CDLL", load)

    for _ in range(3):
        runtime_gc.flush_thread_allocator_cache()

    # 线程池每个任务结束都可能调用，C 库只加载一次
    assert loads == ["load"]
    assert len(mallctl.calls) == 3


def test_thread_cache_flush_is_a_no_op_without_jemalloc(linux, monkeypatch):
    trim = _FakeSymbol()
    _use_lib(monkeypatch, _FakeLib(malloc_trim=trim))

    assert runtime_gc.flush_thread_allocator_cache() is False
    assert trim.calls == []



def test_background_thread_enabled_at_runtime(linux, monkeypatch):
    monkeypatch.setenv("MALLOC_CONF", "narenas:8,dirty_decay_ms:5000")
    mallctl = _FakeSymbol()
    _use_lib(monkeypatch, _FakeLib(mallctl=mallctl))

    assert runtime_gc.configure_allocator_background_thread() is True
    assert [call[0] for call in mallctl.calls] == [b"max_background_threads", b"background_thread"]
    assert all(call[1] is None and call[2] is None for call in mallctl.calls)
    assert runtime_gc.os.environ["MALLOC_CONF"] == "narenas:8,dirty_decay_ms:5000"


def test_inherited_background_thread_conf_is_stripped(linux, monkeypatch):
    monkeypatch.setenv(
        "MALLOC_CONF",
        "background_thread:true,max_background_threads:1,narenas:8,dirty_decay_ms:5000,muzzy_decay_ms:0",
    )
    mallctl = _FakeSymbol()
    _use_lib(monkeypatch, _FakeLib(mallctl=mallctl))

    assert runtime_gc.configure_allocator_background_thread() is False
    assert mallctl.calls == []
    assert runtime_gc.os.environ["MALLOC_CONF"] == "narenas:8,dirty_decay_ms:5000,muzzy_decay_ms:0"


def test_conf_with_only_background_thread_is_removed(linux, monkeypatch):
    monkeypatch.setenv("MALLOC_CONF", "background_thread:false")
    _use_lib(monkeypatch, _FakeLib(mallctl=_FakeSymbol()))

    assert runtime_gc.configure_allocator_background_thread() is False
    assert "MALLOC_CONF" not in runtime_gc.os.environ


def test_background_thread_is_a_no_op_without_jemalloc(linux, monkeypatch):
    monkeypatch.delenv("MALLOC_CONF", raising=False)
    _use_lib(monkeypatch, _FakeLib(malloc_trim=_FakeSymbol()))

    assert runtime_gc.configure_allocator_background_thread() is False
