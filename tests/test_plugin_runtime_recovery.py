"""
插件依赖安装同步、异步入口的恢复决策和资源清理回归。
"""

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.adapters.system.package import PackageInstallRequest, PackageInstallStrategy
from app.adapters.system.plugin import health as health_module
from app.adapters.system.plugin.health import PluginRuntimeHealth

_HEALTHY = {"核心依赖导入检查": (True, "ok")}
_BROKEN = {"核心依赖导入检查": (False, "new failure")}
_OLD_FAILURE = {"核心依赖导入检查": (False, "old failure")}
_REGRESSION = "核心依赖导入检查失败：new failure"


@dataclass(frozen=True)
class _RecoveryCase:
    """
    一次安装在外部边界的固定应答与预期结果。

    :param installs: 各安装策略返回的成功标志和输出
    :param health: 安装前、安装后及修复后的有序健康快照
    :param repairs: 各修复策略返回的成功标志和输出
    :param result: 对外返回的完整成功标志和说明
    :param events: 必须保持的安装、探测、修复和导入刷新顺序
    """

    installs: tuple[tuple[bool, str], ...]
    health: tuple[dict[str, tuple[bool, str]], ...]
    repairs: tuple[tuple[bool, str], ...]
    result: tuple[bool, str]
    events: tuple[str, ...]


_CASES = [
    pytest.param(_RecoveryCase(
        ((True, "installed"),), (_HEALTHY, _HEALTHY), (), (True, "installed"),
        ("health", "install", "health", "refresh"),
    ), id="healthy"),
    pytest.param(_RecoveryCase(
        ((True, "installed"),), (_OLD_FAILURE, _OLD_FAILURE), (), (True, "installed"),
        ("health", "install", "health", "refresh"),
    ), id="existing-failure"),
    pytest.param(_RecoveryCase(
        ((False, "offline"), (True, "installed")), (_HEALTHY, _HEALTHY, _HEALTHY), (),
        (True, "installed"), ("health", "install", "health", "install", "health", "refresh"),
    ), id="fallback"),
    pytest.param(_RecoveryCase(
        ((False, "offline"), (False, "still offline")), (_HEALTHY, _HEALTHY, _HEALTHY), (),
        (False, "[UV] 所有策略均安装依赖失败：still offline"),
        ("health", "install", "health", "install", "health"),
    ), id="exhausted"),
    pytest.param(_RecoveryCase(
        ((False, ""), (False, "")), (_HEALTHY, _HEALTHY, _HEALTHY), (),
        (False, "[UV] 所有策略均安装依赖失败，请检查网络连接、包源配置或插件依赖约束"),
        ("health", "install", "health", "install", "health"),
    ), id="empty-errors"),
    pytest.param(_RecoveryCase(
        ((True, "installed"),), (_HEALTHY, _BROKEN, _HEALTHY), ((True, "repaired"),),
        (False, f"依赖安装后运行环境自检失败，已自动恢复主程序依赖：{_REGRESSION}"),
        ("health", "install", "health", "repair", "refresh", "health", "refresh"),
    ), id="successful-install-restored"),
    pytest.param(_RecoveryCase(
        ((True, "installed"),), (_HEALTHY, _BROKEN), ((False, "repair failed"),),
        (False, "依赖安装后运行环境自检失败，且自动恢复主程序依赖失败：repair failed"),
        ("health", "install", "health", "repair"),
    ), id="successful-install-repair-failed"),
    pytest.param(_RecoveryCase(
        ((True, "installed"),), (_HEALTHY, _BROKEN, _BROKEN), ((True, "repaired"),),
        (False, f"依赖安装后运行环境自检失败，恢复主程序依赖后仍异常：{_REGRESSION}"),
        ("health", "install", "health", "repair", "refresh", "health"),
    ), id="successful-install-still-broken"),
    pytest.param(_RecoveryCase(
        ((False, "install failed"),), (_HEALTHY, _BROKEN, _HEALTHY), ((True, "repaired"),),
        (False, "策略 install-0 安装依赖失败：install failed；主运行环境已恢复"),
        ("health", "install", "health", "repair", "refresh", "health"),
    ), id="failed-install-restored-stops-fallback"),
    pytest.param(_RecoveryCase(
        ((False, "install failed"),), (_HEALTHY, _BROKEN), ((False, "repair failed"),),
        (False, f"策略 install-0 安装依赖失败：install failed；插件依赖安装失败后主运行环境异常，且恢复失败：{_REGRESSION}; repair failed"),
        ("health", "install", "health", "repair"),
    ), id="failed-install-repair-failed"),
    pytest.param(_RecoveryCase(
        ((False, "install failed"),), (_HEALTHY, _BROKEN, _BROKEN), ((True, "repaired"),),
        (False, f"策略 install-0 安装依赖失败：install failed；插件依赖安装失败后主运行环境异常，恢复后仍异常：{_REGRESSION}"),
        ("health", "install", "health", "repair", "refresh", "health"),
    ), id="failed-install-still-broken"),
]


@pytest.fixture
def recovery_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    隔离依赖清单、约束文件、安装锁及子进程，保留真实安装与恢复编排。

    :param tmp_path: 本用例独占的临时目录
    :param monkeypatch: 在用例结束时撤销边界替换的夹具
    :return: 清单路径、约束路径、独占安装锁以及捕获的日志
    """
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("demo\n", encoding="utf-8")
    constraints = tmp_path / "constraints.txt"
    lock = threading.Lock()
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(PluginRuntimeHealth, "_package_install_lock", lock)
    monkeypatch.setattr(health_module, "is_free_threaded_runtime", lambda: False)
    monkeypatch.setattr(health_module, "get_runtime_setting", lambda key: {
        "ROOT_PATH": tmp_path, "CONFIG_PATH": tmp_path, "TEMP_PATH": tmp_path,
        "PACKAGE_CACHE_PATH": tmp_path, "PIP_PROXY": "", "PROXY_HOST": "",
    }[key])
    monkeypatch.setattr(PluginRuntimeHealth, "_PluginRuntimeHealth__get_installed_packages", lambda: {})
    monkeypatch.setattr(PluginRuntimeHealth, "_PluginRuntimeHealth__get_protected_runtime_packages", lambda _: {"fastapi": "1"})
    monkeypatch.setattr(PluginRuntimeHealth, "_PluginRuntimeHealth__validate_runtime_dependency_conflicts", lambda *_: (True, ""))

    def create_constraints(_packages):
        """
        在临时目录生成约束，检查后续路径是否承担清理责任。
        """
        constraints.write_text("fastapi==1\n", encoding="utf-8")
        return constraints

    monkeypatch.setattr(PluginRuntimeHealth, "_PluginRuntimeHealth__create_runtime_constraints_file", create_constraints)
    monkeypatch.setattr(health_module.logger, "warning", lambda message: logs.append(("warning", message)))
    monkeypatch.setattr(health_module.logger, "error", lambda message: logs.append(("error", message)))
    return manifest, constraints, lock, logs


def _patch_runtime(monkeypatch, environment, case: _RecoveryCase) -> list[str]:
    """
    在子进程和健康探测边界重放固定应答，并记录持锁期间的操作顺序。

    :param monkeypatch: 自动恢复边界方法的夹具
    :param environment: 隔离的清单、约束、锁与日志
    :param case: 完整应答序列及预期结果
    :return: 按真实发生顺序追加的操作列表
    """
    _, constraints, lock, _ = environment
    events: list[str] = []
    installs, repairs, health = iter(case.installs), iter(case.repairs), iter(case.health)

    def strategies(request: PackageInstallRequest, dry_run: bool = False) -> list[PackageInstallStrategy]:
        """
        返回离线命令，保持真实修复目标和安装策略切换可观察。
        """
        assert not dry_run
        kind = "repair" if request.purpose == "runtime-repair" else "install"
        if kind == "repair":
            assert request.dependency_files == (constraints,)
        else:
            assert request.constraints_file == constraints
        return [PackageInstallStrategy(f"{kind}-{index}", [kind], {}, [kind])
                for index in range(1 if kind == "repair" else 2)]

    def execute(command, **kwargs):
        """
        检查安装与恢复持有同一锁，禁止启动真实子进程。
        """
        assert lock.locked() and constraints.exists()
        assert kwargs.get("timeout") in (None, 300)
        kind = command[0]
        events.append(kind)
        return next(repairs if kind == "repair" else installs)

    async def execute_async(command, **kwargs):
        """
        复用边界应答，同时要求异步执行保留原有超时。
        """
        assert kwargs["timeout"] == 300
        return execute(command, **kwargs)

    def probe():
        """
        重放健康快照，若多探测或少探测则由序列与事件断言识别。
        """
        assert lock.locked()
        events.append("health")
        return next(health)

    async def probe_async():
        """
        通过相同快照验证异步恢复决策。
        """
        return probe()

    monkeypatch.setattr(health_module, "build_package_install_strategies", strategies)
    monkeypatch.setattr(health_module.SystemUtils, "execute_with_subprocess", execute)
    monkeypatch.setattr(health_module.SystemUtils, "execute_with_subprocess_async", execute_async)
    monkeypatch.setattr(PluginRuntimeHealth, "_PluginRuntimeHealth__run_runtime_healthcheck", probe)
    monkeypatch.setattr(PluginRuntimeHealth, "_PluginRuntimeHealth__async_run_runtime_healthcheck", probe_async)
    monkeypatch.setattr(PluginRuntimeHealth, "_PluginRuntimeHealth__refresh_import_system", lambda: events.append("refresh"))
    return events


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("case", _CASES)
def test_install_recovery_contract(recovery_environment, monkeypatch, case, use_async) -> None:
    """
    两种入口保持既有结果、恢复顺序、停止条件及诊断差异。

    :param recovery_environment: 隔离的安装环境
    :param monkeypatch: 自动恢复子进程和健康检查的夹具
    :param case: 参数化的外部应答及预期合同
    :param use_async: 是否通过异步公开入口执行
    """
    manifest, constraints, lock, logs = recovery_environment
    events = _patch_runtime(monkeypatch, recovery_environment, case)
    if use_async:
        result = asyncio.run(PluginRuntimeHealth().async_install_packages_with_fallback(manifest))
    else:
        result = PluginRuntimeHealth.install_packages_with_fallback(manifest)
    assert result == case.result
    assert tuple(events) == case.events
    assert not constraints.exists()
    assert not lock.locked()
    remaining = [message for _, message in logs if "安装前已有的运行环境异常仍然存在" in message]
    assert bool(remaining) == (not use_async and case.health == (_OLD_FAILURE, _OLD_FAILURE))
    restored_errors = [message for _, message in logs if "主程序依赖恢复后仍未通过健康检查" in message]
    assert bool(restored_errors) == (not use_async and case.installs[0][0] and case.health[-1] == _BROKEN and bool(case.repairs) and case.repairs[0][0])


@pytest.mark.parametrize("stage", ["baseline", "install", "post-install", "repair", "post-repair"])
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_install_exception_releases_resources(recovery_environment, monkeypatch, stage, use_async) -> None:
    """
    探测、安装和恢复异常按原样传播，始终释放约束文件与安装锁。

    :param recovery_environment: 隔离的安装环境
    :param monkeypatch: 自动恢复边界方法的夹具
    :param stage: 注入异常的外部操作阶段
    :param use_async: 是否执行异步公开入口
    """
    manifest, constraints, lock, _ = recovery_environment
    case = _RecoveryCase(((True, "ok"),), (_HEALTHY, _BROKEN, _HEALTHY),
                         ((True, "ok"),), (False, ""), ())
    _patch_runtime(monkeypatch, recovery_environment, case)
    failure = RuntimeError(f"{stage} failed")
    probe = PluginRuntimeHealth._PluginRuntimeHealth__run_runtime_healthcheck
    execute = health_module.SystemUtils.execute_with_subprocess
    phases = iter(("baseline", "post-install", "post-repair"))

    def failing_probe():
        """
        在指定的健康探测阶段注入同一异常对象。
        """
        if next(phases) == stage:
            raise failure
        return probe()

    def failing_execute(command, **kwargs):
        """
        在安装或修复子进程边界注入异常。
        """
        if command[0] == stage:
            raise failure
        return execute(command, **kwargs)

    async def failing_probe_async():
        """
        保持异步探测入口的异常传播行为。
        """
        return failing_probe()

    async def failing_execute_async(command, **kwargs):
        """
        保持异步子进程入口的异常传播行为。
        """
        return failing_execute(command, **kwargs)

    monkeypatch.setattr(PluginRuntimeHealth, "_PluginRuntimeHealth__run_runtime_healthcheck", failing_probe)
    monkeypatch.setattr(PluginRuntimeHealth, "_PluginRuntimeHealth__async_run_runtime_healthcheck", failing_probe_async)
    monkeypatch.setattr(health_module.SystemUtils, "execute_with_subprocess", failing_execute)
    monkeypatch.setattr(health_module.SystemUtils, "execute_with_subprocess_async", failing_execute_async)
    with pytest.raises(RuntimeError) as raised:
        if use_async:
            asyncio.run(PluginRuntimeHealth().async_install_packages_with_fallback(manifest))
        else:
            PluginRuntimeHealth.install_packages_with_fallback(manifest)
    assert raised.value is failure
    assert not constraints.exists()
    assert not lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["baseline", "install", "post-install", "repair", "post-repair"])
async def test_cancelled_recovery_releases_resources(recovery_environment, monkeypatch, stage) -> None:
    """
    在真实等待点取消安装或恢复，取消不得转换成失败结果或继续下一策略。

    :param recovery_environment: 隔离的安装环境
    :param monkeypatch: 自动恢复子进程和探测的夹具
    :param stage: 等待取消的外部操作阶段
    """
    manifest, constraints, lock, _ = recovery_environment
    case = _RecoveryCase(((True, "ok"),), (_HEALTHY, _BROKEN, _HEALTHY),
                         ((True, "ok"),), (False, ""), ())
    events = _patch_runtime(monkeypatch, recovery_environment, case)
    probe = PluginRuntimeHealth._PluginRuntimeHealth__run_runtime_healthcheck
    execute = health_module.SystemUtils.execute_with_subprocess
    phases = iter(("baseline", "post-install", "post-repair"))
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def wait_for_cancel(phase: str) -> None:
        """
        使用屏障精确定位取消时点，收尾标记用于验证子操作先退出。
        """
        if phase == stage:
            entered.set()
            try:
                await asyncio.Future()
            finally:
                exited.set()

    async def blocked_probe():
        """
        在目标健康探测之前暂停。
        """
        await wait_for_cancel(next(phases))
        return probe()

    async def blocked_execute(command, **kwargs):
        """
        在目标子进程边界暂停，不启动真实安装器。
        """
        await wait_for_cancel(command[0])
        return execute(command, **kwargs)

    monkeypatch.setattr(PluginRuntimeHealth, "_PluginRuntimeHealth__async_run_runtime_healthcheck", blocked_probe)
    monkeypatch.setattr(health_module.SystemUtils, "execute_with_subprocess_async", blocked_execute)
    task = asyncio.create_task(PluginRuntimeHealth().async_install_packages_with_fallback(manifest))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        before_cancel = list(events)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert exited.is_set()
        assert events == before_cancel
        assert not constraints.exists()
        assert not lock.locked()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_install_waiting_for_lock_preserves_owner(recovery_environment, monkeypatch) -> None:
    """
    排队时取消只清理自己的约束，不能释放其他安装者持有的锁。

    :param recovery_environment: 隔离的安装环境
    :param monkeypatch: 自动恢复预检边界的夹具
    """
    manifest, constraints, lock, _ = recovery_environment
    ready = asyncio.Event()

    async def preflight(_request):
        """
        在创建约束之后标记就绪，下一步将等待已被占用的锁。
        """
        ready.set()
        return None

    monkeypatch.setattr(health_module, "build_package_install_strategies", lambda _request: [])
    monkeypatch.setattr(PluginRuntimeHealth, "_PluginRuntimeHealth__async_free_threaded_preflight", preflight)
    lock.acquire()
    task = asyncio.create_task(PluginRuntimeHealth().async_install_packages_with_fallback(manifest))
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        assert constraints.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert lock.locked()
        assert not constraints.exists()
    finally:
        lock.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_empty_install_strategies_preserve_failure(recovery_environment, monkeypatch, use_async) -> None:
    """
    无可用策略时保留原有失败说明，不启动安装或恢复。

    :param recovery_environment: 隔离的安装环境
    :param monkeypatch: 自动恢复策略工厂和边界方法的夹具
    :param use_async: 是否执行异步公开入口
    """
    manifest, constraints, lock, _ = recovery_environment
    case = _RecoveryCase((), (_HEALTHY,), (), (False, ""), ())
    events = _patch_runtime(monkeypatch, recovery_environment, case)
    monkeypatch.setattr(health_module, "build_package_install_strategies", lambda _request: [])
    if use_async:
        result = asyncio.run(PluginRuntimeHealth().async_install_packages_with_fallback(manifest))
    else:
        result = PluginRuntimeHealth.install_packages_with_fallback(manifest)
    assert result == (False, "[UV] 所有策略均安装依赖失败，请检查网络连接、包源配置或插件依赖约束")
    assert events == ["health"]
    assert not constraints.exists()
    assert not lock.locked()
