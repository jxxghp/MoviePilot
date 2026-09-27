"""分配器空闲页归还：按进程实际加载的分配器选择 jemalloc purge 或 glibc malloc_trim。"""

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
