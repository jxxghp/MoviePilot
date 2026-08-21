"""MoviePilot Uvicorn factory、reload 与生产服务器入口测试。"""

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import factory, main
from app.runtime.topology import UnsupportedProcessTopologyError


PROJECT_ROOT = Path(__file__).parents[1]


def test_create_app_does_not_start_plugin_manager_or_threads(monkeypatch):
    """ASGI factory 只构建应用结构，不得在创建阶段物化插件运行时。"""
    plugin_manager = MagicMock(side_effect=AssertionError("plugin runtime started"))
    monkeypatch.setattr(factory, "PluginManager", plugin_manager)
    threads_before = threading.active_count()

    created = factory.create_app()

    assert created is not factory.app
    assert threading.active_count() == threads_before
    plugin_manager.assert_not_called()


def test_production_entry_uses_custom_single_process_server(monkeypatch):
    """生产单 worker 保留发布协作停止标志的自定义 Server。"""
    server = MagicMock()
    monkeypatch.setattr(main.settings, "DEV", False)
    monkeypatch.setattr(main.settings, "API_WORKERS", 1)
    monkeypatch.setattr(main, "create_server", MagicMock(return_value=server))
    uvicorn_run = MagicMock()
    monkeypatch.setattr(main.uvicorn, "run", uvicorn_run)

    main.run_api_server()

    assert main.Server is server
    server.run.assert_called_once_with()
    uvicorn_run.assert_not_called()


def test_development_reload_uses_import_string_factory(monkeypatch):
    """开发 reload 必须让 Uvicorn 重新导入 factory，而不是序列化 app 实例。"""
    monkeypatch.setattr(main.settings, "DEV", True)
    monkeypatch.setattr(main.settings, "API_WORKERS", 1)
    uvicorn_run = MagicMock()
    monkeypatch.setattr(main.uvicorn, "run", uvicorn_run)
    monkeypatch.setattr(main, "Server", MagicMock())

    main.run_api_server()

    assert main.Server is None
    uvicorn_run.assert_called_once_with(
        main.APP_FACTORY,
        factory=True,
        host=main.settings.HOST,
        port=main.settings.PORT,
        reload=True,
        workers=1,
        timeout_graceful_shutdown=60,
    )


def test_safe_mode_multi_worker_uses_import_string_factory(monkeypatch):
    """安全模式多 worker 由 Uvicorn supervisor 创建独立 ASGI factory 实例。"""
    monkeypatch.setattr(main.settings, "DEV", False)
    monkeypatch.setattr(main.settings, "MOVIEPILOT_SAFE_MODE", True)
    monkeypatch.setattr(main.settings, "API_WORKERS", 2)
    uvicorn_run = MagicMock()
    monkeypatch.setattr(main.uvicorn, "run", uvicorn_run)

    main.run_api_server()

    assert uvicorn_run.call_args.kwargs["factory"] is True
    assert uvicorn_run.call_args.kwargs["reload"] is False
    assert uvicorn_run.call_args.kwargs["workers"] == 2


def test_reload_and_multiple_workers_are_rejected_together(monkeypatch):
    """Uvicorn 不支持的 reload + workers 组合必须给出明确错误。"""
    monkeypatch.setattr(main.settings, "DEV", True)
    monkeypatch.setattr(main.settings, "MOVIEPILOT_SAFE_MODE", True)
    monkeypatch.setattr(main.settings, "API_WORKERS", 2)
    uvicorn_run = MagicMock()
    monkeypatch.setattr(main.uvicorn, "run", uvicorn_run)

    with pytest.raises(UnsupportedProcessTopologyError, match="不能同时启用"):
        main.run_api_server()

    uvicorn_run.assert_not_called()


def test_request_shutdown_is_safe_before_server_creation(monkeypatch):
    """数据库准备或 reload supervisor 阶段收到退出请求时不依赖 Server 已创建。"""
    stop_system = MagicMock()
    monkeypatch.setattr(main.global_vars, "stop_system", stop_system)
    monkeypatch.setattr(main, "Server", None)

    main.request_shutdown()

    stop_system.assert_called_once_with()


def test_production_server_preserves_shutdown_requested_before_creation(
    monkeypatch,
):
    """数据库准备期间收到的停止请求必须传递给随后创建的生产 Server。"""
    stop_event = threading.Event()
    stop_event.set()
    monkeypatch.setattr(main.global_vars, "STOP_EVENT", stop_event)

    server = main.create_server()

    assert server.should_exit is True


def test_local_launcher_keeps_module_entrypoint():
    """本地开发脚本继续通过 app.main 进入统一启动准备流程。"""
    script = (PROJECT_ROOT / "scripts" / "start-local.sh").read_text(
        encoding="utf-8"
    )

    assert 'exec "$VENV_PYTHON" -m app.main' in script
