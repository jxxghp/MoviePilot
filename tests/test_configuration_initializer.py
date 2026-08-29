"""配置快照启动顺序测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.configuration import configure_runtime_settings
from app.startup.initializers import modules as modules_initializer


class _InlineWorker:
    """按提交顺序执行配置加载操作。"""

    async def run(self, operation):
        """执行并返回操作结果。"""
        return operation()


class _MutableSettings:
    """提供运行时配置代理回归测试所需的最小可变设置实现。"""

    def __init__(self) -> None:
        """初始化一项可读写的部署设置。"""
        self.VALUE = "before"

    def model_dump(self, *, include=None, exclude=None):
        """导出测试设置快照。"""
        values = {"VALUE": self.VALUE}
        if include is not None:
            values = {key: value for key, value in values.items() if key in include}
        if exclude is not None:
            values = {key: value for key, value in values.items() if key not in exclude}
        return values

    def update_settings(self, env):
        """批量更新测试设置。"""
        for key, value in env.items():
            setattr(self, key, value)
        return {key: (True, "") for key in env}

    def update_setting(self, key, value):
        """更新单项测试设置。"""
        setattr(self, key, value)
        return True, ""


@pytest.mark.asyncio
async def test_network_test_transport_enforces_safe_http_options(monkeypatch) -> None:
    """组合根适配器必须固定证书校验、超时和手动重定向策略。"""
    captured = {}
    response = SimpleNamespace(status_code=200, headers={}, text="ok")

    class _RequestUtils:
        """捕获组合根网络探测适配器使用的 HTTP 参数。"""

        def __init__(self, **kwargs) -> None:
            """记录通用请求工具的构造参数。"""
            captured["options"] = kwargs

        async def get_res(self, url, allow_redirects=True):
            """记录请求地址和重定向开关并返回固定响应。"""
            captured["url"] = url
            captured["allow_redirects"] = allow_redirects
            return response

    monkeypatch.setattr(modules_initializer, "AsyncRequestUtils", _RequestUtils)

    result = await modules_initializer._NetworkTestTransportAdapter().get(
        "https://example.com/health",
        proxy="http://proxy.example:7890",
        headers={"Authorization": "Bearer test"},
        user_agent="MoviePilot-Test",
    )

    assert result is response
    assert captured == {
        "url": "https://example.com/health",
        "allow_redirects": False,
        "options": {
            "proxies": "http://proxy.example:7890",
            "headers": {"Authorization": "Bearer test"},
            "timeout": 10,
            "ua": "MoviePilot-Test",
            "verify": True,
            "follow_redirects": False,
        },
    }


def test_runtime_settings_service_uses_legacy_settings_from_startup_root(monkeypatch) -> None:
    """组合根装配的设置服务应直接读写唯一部署配置对象。"""
    legacy_settings = _MutableSettings()
    monkeypatch.setattr(modules_initializer, "legacy_settings", legacy_settings)

    service = modules_initializer._build_runtime_settings_service()
    configure_runtime_settings(service)

    assert service.snapshot(include={"VALUE"}) == {"VALUE": "before"}
    assert service.update("VALUE", "after") == (True, "")
    assert service.snapshot(include={"VALUE"}) == {"VALUE": "after"}


@pytest.mark.asyncio
async def test_configuration_services_publish_after_both_snapshots_load(
    monkeypatch,
) -> None:
    """两个完整快照加载成功前不发布任一配置服务。"""
    events = []

    class _SystemConfig:
        """记录系统配置快照加载顺序。"""

        def load_snapshot(self):
            """登记系统快照已加载。"""
            events.append("load-system")

    class _UserConfig:
        """记录用户配置快照加载顺序。"""

        def load_snapshot(self):
            """登记用户快照已加载。"""
            events.append("load-user")

    monkeypatch.setattr(modules_initializer, "SystemConfigOper", _SystemConfig)
    monkeypatch.setattr(
        modules_initializer,
        "TransactionalUserConfigurationRepository",
        lambda _session_factory: _UserConfig(),
    )
    monkeypatch.setattr(
        modules_initializer,
        "configure_system_config",
        lambda _service: events.append("publish-system"),
    )
    monkeypatch.setattr(
        modules_initializer,
        "configure_user_configuration",
        lambda _service: events.append("publish-user"),
    )

    await modules_initializer._initialize_configuration_services(_InlineWorker())

    assert events == [
        "load-system",
        "load-user",
        "publish-system",
        "publish-user",
    ]


@pytest.mark.asyncio
async def test_configuration_load_failure_does_not_publish_partial_service(
    monkeypatch,
) -> None:
    """任一快照加载失败时不发布半套配置服务。"""
    published = []

    class _SystemConfig:
        """提供成功的系统配置快照加载桩。"""

        def load_snapshot(self):
            """模拟系统配置加载成功。"""
            return None

    class _UserConfig:
        """提供失败的用户配置快照加载桩。"""

        def load_snapshot(self):
            """模拟用户配置加载失败。"""
            raise RuntimeError("load failed")

    monkeypatch.setattr(modules_initializer, "SystemConfigOper", _SystemConfig)
    monkeypatch.setattr(
        modules_initializer,
        "TransactionalUserConfigurationRepository",
        lambda _session_factory: _UserConfig(),
    )
    monkeypatch.setattr(
        modules_initializer,
        "configure_system_config",
        lambda service: published.append(service),
    )
    monkeypatch.setattr(
        modules_initializer,
        "configure_user_configuration",
        lambda service: published.append(service),
    )

    with pytest.raises(RuntimeError, match="load failed"):
        await modules_initializer._initialize_configuration_services(_InlineWorker())

    assert published == []


@pytest.mark.asyncio
async def test_modules_startup_failure_stops_database_worker(monkeypatch) -> None:
    """模块组合根启动失败时立即关闭已创建的数据库 worker。"""
    monkeypatch.setattr(
        modules_initializer,
        "_initialize_modules",
        AsyncMock(side_effect=RuntimeError("startup failed")),
    )
    monkeypatch.setattr(modules_initializer, "stop_message", lambda: True)
    stop_worker = AsyncMock()
    monkeypatch.setattr(modules_initializer, "stop_database_worker", stop_worker)

    with pytest.raises(RuntimeError, match="startup failed"):
        await modules_initializer.init_modules()

    stop_worker.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_modules_startup_failure_preserves_original_error_when_cleanup_fails(
        monkeypatch,
) -> None:
    """数据库任务清理失败时仍向上层保留原始启动异常。"""
    startup_error = RuntimeError("startup failed")
    monkeypatch.setattr(
        modules_initializer,
        "_initialize_modules",
        AsyncMock(side_effect=startup_error),
    )
    monkeypatch.setattr(modules_initializer, "stop_message", lambda: True)
    monkeypatch.setattr(
        modules_initializer,
        "stop_database_worker",
        AsyncMock(side_effect=RuntimeError("cleanup failed")),
    )

    with pytest.raises(RuntimeError) as raised:
        await modules_initializer.init_modules()

    assert raised.value is startup_error


@pytest.mark.asyncio
async def test_database_worker_owner_is_retained_when_shutdown_fails(monkeypatch) -> None:
    """数据库 worker 关闭失败时保留 owner，允许后续重试或诊断。"""

    class _FailingWorker:
        """模拟无法完成关闭的数据库 worker。"""

        async def shutdown(self):
            """抛出关闭错误以验证 owner 保留。"""
            raise RuntimeError("shutdown failed")

    worker = _FailingWorker()
    monkeypatch.setattr(modules_initializer, "_database_worker", worker)

    with pytest.raises(RuntimeError, match="shutdown failed"):
        await modules_initializer.stop_database_worker()

    assert modules_initializer._database_worker is worker
