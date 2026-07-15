import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import Mock

import pytest
from packaging.version import Version
from watchfiles import Change

from app.core.event import Event, eventmanager
from app.core.plugin import PluginManager
from app.helper.plugin import PluginHelper
from app.scheduler import Scheduler
from app.schemas.types import EventType, SystemConfigKey
from app.utils.singleton import Singleton


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def _build_local_plugin_repo(tmp_path: Path) -> tuple[Path, Path]:
    """构造带运行资产、构建依赖和系统版本要求的本地 v2 插件仓库。"""
    repo_path = tmp_path / "local-plugins"
    source_dir = repo_path / "plugins.v2" / "demoplugin"
    source_file = source_dir / "__init__.py"
    source_dir.mkdir(parents=True)
    source_file.write_text(
        "from app.plugins import _PluginBase\n"
        "class DemoPlugin(_PluginBase):\n"
        "    plugin_name = 'Demo'\n",
        encoding="utf-8",
    )
    remote_entry = source_dir / "dist" / "assets" / "remoteEntry.js"
    remote_entry.parent.mkdir(parents=True)
    remote_entry.write_text("export default {}\n", encoding="utf-8")
    dependency_file = source_dir / "node_modules" / "example" / "index.js"
    dependency_file.parent.mkdir(parents=True)
    dependency_file.write_text("module.exports = {}\n", encoding="utf-8")
    (repo_path / "package.v2.json").write_text(
        '{"DemoPlugin": {"version": "1.0.0", "system_version": ">=2.13.11"}}',
        encoding="utf-8",
    )
    return repo_path, source_file


def _configure_local_watcher(
    monkeypatch,
    tmp_path: Path,
    repo_path: Path,
    changes: set[tuple[Change, str]],
    *,
    dev: bool = True,
) -> None:
    """为单批次本地插件文件监测提供完整运行配置。"""
    settings_stub = SimpleNamespace(
        DEV=dev,
        PLUGIN_AUTO_RELOAD=True,
        PLUGIN_LOCAL_REPO_PATHS=str(repo_path),
        ROOT_PATH=tmp_path,
        VERSION_FLAG="v2",
    )
    monkeypatch.setattr("app.core.plugin.settings", settings_stub)
    monkeypatch.setattr("app.helper.plugin.settings", settings_stub)
    monkeypatch.setattr("app.core.plugin.watch", lambda *_args, **_kwargs: iter([changes]))


def _set_running_render_mode(
    plugin_manager: PluginManager,
    render_mode: str,
    dist_path: str,
) -> None:
    """注册测试所需的运行态插件联邦渲染声明。"""
    plugin_manager.running_plugins["DemoPlugin"] = SimpleNamespace(
        get_render_mode=lambda: (render_mode, dist_path),
    )


class _FakeSchedulerBackend:
    """提供插件服务增删所需的最小 APScheduler 契约。"""

    def __init__(self, job_ids: list[str]):
        self.jobs = {job_id: {"id": job_id} for job_id in job_ids}

    def get_jobs(self):
        """返回当前注册的 APScheduler job。"""
        return [SimpleNamespace(id=job_id) for job_id in self.jobs]

    def remove_job(self, job_id: str) -> None:
        """移除指定 APScheduler job。"""
        self.jobs.pop(job_id)

    def add_job(self, func, trigger, **kwargs) -> None:
        """记录并替换指定 APScheduler job。"""
        self.jobs[kwargs["id"]] = {"func": func, "trigger": trigger, **kwargs}


def _build_scheduler_for_plugin_reload(jobs: dict, backend) -> Scheduler:
    """构造不启动后台线程的插件服务 Scheduler。"""
    scheduler = object.__new__(Scheduler)
    scheduler._lock = threading.RLock()
    scheduler._jobs = jobs
    scheduler._scheduler = backend
    return scheduler


def test_dev_local_plugin_candidate_keeps_hot_sync_allowed_when_system_version_lags(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """DEV 本地源码候选保留热同步资格，系统版本差异只作为兼容性提示。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)
    runtime_dir = tmp_path / "app" / "plugins" / "demoplugin"

    monkeypatch.setattr("app.core.plugin.settings", SimpleNamespace(DEV=True, ROOT_PATH=tmp_path))
    monkeypatch.setattr("app.helper.plugin.settings.PLUGIN_LOCAL_REPO_PATHS", str(repo_path))
    monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.13.10"))
    monkeypatch.setattr(
        "app.core.plugin.SystemConfigOper.get",
        lambda _self, key: ["DemoPlugin"] if key == SystemConfigKey.UserInstalledPlugins else None,
    )

    candidate = plugin_manager._get_local_plugin_candidate_from_path(source_file)

    assert candidate["system_version_compatible"] is False
    assert candidate.get("compatible") is not False
    assert plugin_manager._sync_local_plugin_if_installed("DemoPlugin", candidate)
    assert (runtime_dir / "__init__.py").read_text(encoding="utf-8") == source_file.read_text(encoding="utf-8")
    assert (runtime_dir / "dist" / "assets" / "remoteEntry.js").is_file()
    assert not (runtime_dir / "node_modules").exists()


def test_local_plugin_candidate_keeps_system_version_gate_outside_dev(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """非 DEV 本地候选继续受主系统版本门禁保护，避免自动热加载绕过安装约束。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)

    monkeypatch.setattr("app.core.plugin.settings", SimpleNamespace(DEV=False, ROOT_PATH=tmp_path))
    monkeypatch.setattr("app.helper.plugin.settings.PLUGIN_LOCAL_REPO_PATHS", str(repo_path))
    monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.13.10"))

    candidate = plugin_manager._get_local_plugin_candidate_from_path(source_file)

    assert candidate["system_version_compatible"] is False
    assert candidate["compatible"] is False
    assert "MoviePilot 版本 >=2.13.11" in candidate["skip_reason"]


def test_local_plugin_sync_without_candidate_respects_system_version_gate(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """未传候选时的本地同步兜底查询也必须遵守系统版本门禁。"""
    repo_path, _source_file = _build_local_plugin_repo(tmp_path)
    runtime_dir = tmp_path / "app" / "plugins" / "demoplugin"
    settings_stub = SimpleNamespace(
        DEV=False,
        ROOT_PATH=tmp_path,
        VERSION_FLAG="v2",
        PLUGIN_LOCAL_REPO_PATHS=str(repo_path),
    )

    monkeypatch.setattr("app.core.plugin.settings", settings_stub)
    monkeypatch.setattr("app.helper.plugin.settings", settings_stub)
    monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.13.10"))
    monkeypatch.setattr(
        "app.core.plugin.SystemConfigOper.get",
        lambda _self, key: ["DemoPlugin"] if key == SystemConfigKey.UserInstalledPlugins else None,
    )

    assert not plugin_manager._sync_local_plugin_if_installed("DemoPlugin")
    assert not runtime_dir.exists()


def test_local_federated_asset_batch_syncs_once_without_python_reload(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """同批联邦资产变化只同步一次运行副本，不触发 Python 热重载。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)
    source_dir = source_file.parent
    chunk_file = source_dir / "dist" / "assets" / "chunk.js"
    chunk_file.write_text("export const chunk = true\n", encoding="utf-8")
    _configure_local_watcher(
        monkeypatch,
        tmp_path,
        repo_path,
        {
            (Change.modified, str(source_dir / "dist" / "assets" / "remoteEntry.js")),
            (Change.modified, str(chunk_file)),
        },
    )
    _set_running_render_mode(plugin_manager, "vue", "dist/assets")
    monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.13.11"))
    monkeypatch.setattr(
        "app.core.plugin.SystemConfigOper.get",
        lambda _self, key: ["DemoPlugin"] if key == SystemConfigKey.UserInstalledPlugins else None,
    )
    sync_spy = Mock(wraps=plugin_manager._sync_local_plugin_if_installed)
    reload_spy = Mock()
    monkeypatch.setattr(plugin_manager, "_sync_local_plugin_if_installed", sync_spy)
    monkeypatch.setattr(plugin_manager, "reload_plugin", reload_spy)

    plugin_manager._run_file_watcher()

    assert sync_spy.call_count == 1
    assert sync_spy.call_args.args[0] == "DemoPlugin"
    assert (tmp_path / "app" / "plugins" / "demoplugin" / "dist" / "assets" / "chunk.js").is_file()
    reload_spy.assert_not_called()


@pytest.mark.parametrize(
    ("render_mode", "dist_path"),
    [
        ("schema", "dist/assets"),
        ("vue", "../assets"),
        ("vue", "dist/../assets"),
        ("vue", "dist\\assets"),
        ("vue", "/tmp/assets"),
    ],
)
def test_local_federated_asset_ignores_non_vue_or_unsafe_render_paths(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
    render_mode: str,
    dist_path: str,
) -> None:
    """非 Vue 模式和越出插件目录的声明路径不参与本地同步。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)
    remote_entry = source_file.parent / "dist" / "assets" / "remoteEntry.js"
    _configure_local_watcher(
        monkeypatch,
        tmp_path,
        repo_path,
        {(Change.modified, str(remote_entry))},
    )
    _set_running_render_mode(plugin_manager, render_mode, dist_path)
    sync_spy = Mock()
    reload_spy = Mock()
    monkeypatch.setattr(plugin_manager, "_sync_local_plugin_if_installed", sync_spy)
    monkeypatch.setattr(plugin_manager, "reload_plugin", reload_spy)

    plugin_manager._run_file_watcher()

    sync_spy.assert_not_called()
    reload_spy.assert_not_called()


def test_local_federated_asset_ignores_change_outside_declared_directory(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """声明目录外的普通文件变化不复制联邦构建产物。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)
    _configure_local_watcher(
        monkeypatch,
        tmp_path,
        repo_path,
        {(Change.modified, str(source_file.parent / "README.md"))},
    )
    _set_running_render_mode(plugin_manager, "vue", "dist/assets")
    sync_spy = Mock()
    reload_spy = Mock()
    monkeypatch.setattr(plugin_manager, "_sync_local_plugin_if_installed", sync_spy)
    monkeypatch.setattr(plugin_manager, "reload_plugin", reload_spy)

    plugin_manager._run_file_watcher()

    sync_spy.assert_not_called()
    reload_spy.assert_not_called()


def test_local_federated_asset_requires_remote_entry(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """联邦入口文件不存在时不复制声明目录中的其它构建产物。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)
    source_dir = source_file.parent
    remote_entry = source_dir / "dist" / "assets" / "remoteEntry.js"
    remote_entry.unlink()
    chunk_file = source_dir / "dist" / "assets" / "chunk.js"
    chunk_file.write_text("export const chunk = true\n", encoding="utf-8")
    _configure_local_watcher(
        monkeypatch,
        tmp_path,
        repo_path,
        {(Change.modified, str(chunk_file))},
    )
    _set_running_render_mode(plugin_manager, "vue", "dist/assets")
    sync_spy = Mock()
    reload_spy = Mock()
    monkeypatch.setattr(plugin_manager, "_sync_local_plugin_if_installed", sync_spy)
    monkeypatch.setattr(plugin_manager, "reload_plugin", reload_spy)

    plugin_manager._run_file_watcher()

    sync_spy.assert_not_called()
    reload_spy.assert_not_called()


def test_local_federated_asset_reads_running_render_mode_for_each_batch(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """每批变化都从运行实例读取当前联邦目录，不缓存旧声明。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)
    source_dir = source_file.parent
    next_entry = source_dir / "next" / "assets" / "remoteEntry.js"
    next_entry.parent.mkdir(parents=True)
    next_entry.write_text("export default {}\n", encoding="utf-8")
    settings_stub = SimpleNamespace(
        DEV=True,
        PLUGIN_AUTO_RELOAD=True,
        PLUGIN_LOCAL_REPO_PATHS=str(repo_path),
        ROOT_PATH=tmp_path,
        VERSION_FLAG="v2",
    )
    monkeypatch.setattr("app.core.plugin.settings", settings_stub)
    monkeypatch.setattr("app.helper.plugin.settings", settings_stub)
    monkeypatch.setattr(
        "app.core.plugin.watch",
        lambda *_args, **_kwargs: iter([
            {(Change.modified, str(source_dir / "dist" / "assets" / "remoteEntry.js"))},
            {(Change.modified, str(next_entry))},
        ]),
    )
    render_mode = Mock(side_effect=[("vue", "dist/assets"), ("vue", "next/assets")])
    plugin_manager.running_plugins["DemoPlugin"] = SimpleNamespace(get_render_mode=render_mode)
    sync_spy = Mock(return_value=True)
    reload_spy = Mock()
    monkeypatch.setattr(plugin_manager, "_sync_local_plugin_if_installed", sync_spy)
    monkeypatch.setattr(plugin_manager, "reload_plugin", reload_spy)

    plugin_manager._run_file_watcher()

    assert render_mode.call_count == 2
    assert sync_spy.call_count == 2
    reload_spy.assert_not_called()


def test_local_federated_asset_respects_non_dev_compatibility_gate(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """非 DEV 自动监测不绕过本地插件的系统版本兼容性门禁。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)
    remote_entry = source_file.parent / "dist" / "assets" / "remoteEntry.js"
    _configure_local_watcher(
        monkeypatch,
        tmp_path,
        repo_path,
        {(Change.modified, str(remote_entry))},
        dev=False,
    )
    _set_running_render_mode(plugin_manager, "vue", "dist/assets")
    monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.13.10"))
    sync_spy = Mock()
    reload_spy = Mock()
    monkeypatch.setattr(plugin_manager, "_sync_local_plugin_if_installed", sync_spy)
    monkeypatch.setattr(plugin_manager, "reload_plugin", reload_spy)

    plugin_manager._run_file_watcher()

    sync_spy.assert_not_called()
    reload_spy.assert_not_called()


def test_runtime_federated_asset_change_does_not_copy_or_reload(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """运行目录中的联邦资产由构建方直接写入，不执行本地仓库复制或 Python 重载。"""
    repo_path, _source_file = _build_local_plugin_repo(tmp_path)
    runtime_dir = tmp_path / "app" / "plugins" / "demoplugin"
    runtime_entry = runtime_dir / "dist" / "assets" / "remoteEntry.js"
    runtime_entry.parent.mkdir(parents=True)
    runtime_entry.write_text("export default {}\n", encoding="utf-8")
    (runtime_dir / "__init__.py").write_text(
        "from app.plugins import _PluginBase\n"
        "class DemoPlugin(_PluginBase):\n"
        "    plugin_name = 'Demo'\n",
        encoding="utf-8",
    )
    generated_python = runtime_entry.parent / "generated.py"
    generated_python.write_text("ASSET = True\n", encoding="utf-8")
    _configure_local_watcher(
        monkeypatch,
        tmp_path,
        repo_path,
        {(Change.modified, str(generated_python))},
    )
    _set_running_render_mode(plugin_manager, "vue", "dist/assets")
    sync_spy = Mock()
    reload_spy = Mock()
    monkeypatch.setattr(plugin_manager, "_sync_local_plugin_if_installed", sync_spy)
    monkeypatch.setattr(plugin_manager, "reload_plugin", reload_spy)

    plugin_manager._run_file_watcher()

    sync_spy.assert_not_called()
    reload_spy.assert_not_called()


def test_local_requirements_change_still_does_not_sync_or_reload(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """依赖文件变化继续只提示重新安装，不触发自动同步或热重载。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)
    requirements_file = source_file.parent / "requirements.txt"
    requirements_file.write_text("example==1.0.0\n", encoding="utf-8")
    _configure_local_watcher(
        monkeypatch,
        tmp_path,
        repo_path,
        {(Change.modified, str(requirements_file))},
    )
    monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.13.11"))
    sync_spy = Mock()
    reload_spy = Mock()
    monkeypatch.setattr(plugin_manager, "_sync_local_plugin_if_installed", sync_spy)
    monkeypatch.setattr(plugin_manager, "reload_plugin", reload_spy)

    plugin_manager._run_file_watcher()

    sync_spy.assert_not_called()
    reload_spy.assert_not_called()


def test_local_python_change_still_syncs_and_reloads_plugin(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """本地 Python 源码变化继续沿用同步后热重载语义。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)
    _configure_local_watcher(
        monkeypatch,
        tmp_path,
        repo_path,
        {(Change.modified, str(source_file))},
    )
    monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.13.11"))
    monkeypatch.setattr(
        "app.core.plugin.SystemConfigOper.get",
        lambda _self, key: ["DemoPlugin"] if key == SystemConfigKey.UserInstalledPlugins else None,
    )
    sync_spy = Mock(wraps=plugin_manager._sync_local_plugin_if_installed)
    reload_spy = Mock()
    monkeypatch.setattr(plugin_manager, "_sync_local_plugin_if_installed", sync_spy)
    monkeypatch.setattr(plugin_manager, "reload_plugin", reload_spy)

    plugin_manager._run_file_watcher()

    assert sync_spy.call_count == 1
    reload_spy.assert_called_once_with("DemoPlugin")


@pytest.mark.parametrize("dist_path", [".", "./"])
def test_local_python_change_rejects_root_federated_path_and_still_reloads(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
    dist_path: str,
) -> None:
    """插件根目录不能作为联邦输出目录，Python 变化仍需同步并热重载。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)
    _configure_local_watcher(
        monkeypatch,
        tmp_path,
        repo_path,
        {(Change.modified, str(source_file))},
    )
    _set_running_render_mode(plugin_manager, "vue", dist_path)
    monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.13.11"))
    monkeypatch.setattr(
        "app.core.plugin.SystemConfigOper.get",
        lambda _self, key: ["DemoPlugin"] if key == SystemConfigKey.UserInstalledPlugins else None,
    )
    sync_spy = Mock(wraps=plugin_manager._sync_local_plugin_if_installed)
    reload_spy = Mock()
    monkeypatch.setattr(plugin_manager, "_sync_local_plugin_if_installed", sync_spy)
    monkeypatch.setattr(plugin_manager, "reload_plugin", reload_spy)

    assert plugin_manager._get_federated_plugin_change(source_file) is None

    plugin_manager._run_file_watcher()

    assert sync_spy.call_count == 1
    reload_spy.assert_called_once_with("DemoPlugin")


def test_local_python_and_federated_changes_share_one_batch_sync(
    tmp_path,
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """同批 Python 与联邦资产变化共用一次复制，并保留 Python 热重载。"""
    repo_path, source_file = _build_local_plugin_repo(tmp_path)
    remote_entry = source_file.parent / "dist" / "assets" / "remoteEntry.js"
    _configure_local_watcher(
        monkeypatch,
        tmp_path,
        repo_path,
        {
            (Change.modified, str(source_file)),
            (Change.modified, str(remote_entry)),
        },
    )
    _set_running_render_mode(plugin_manager, "vue", "dist/assets")
    monkeypatch.setattr(PluginHelper, "get_current_system_version", lambda: Version("2.13.11"))
    monkeypatch.setattr(
        "app.core.plugin.SystemConfigOper.get",
        lambda _self, key: ["DemoPlugin"] if key == SystemConfigKey.UserInstalledPlugins else None,
    )
    sync_spy = Mock(wraps=plugin_manager._sync_local_plugin_if_installed)
    reload_spy = Mock()
    monkeypatch.setattr(plugin_manager, "_sync_local_plugin_if_installed", sync_spy)
    monkeypatch.setattr(plugin_manager, "reload_plugin", reload_spy)

    plugin_manager._run_file_watcher()

    assert sync_spy.call_count == 1
    reload_spy.assert_called_once_with("DemoPlugin")


def test_plugin_reload_refreshes_scheduler_services_idempotently(monkeypatch):
    """插件重载事件必须按当前服务拓扑幂等刷新 Scheduler。"""
    current_func = Mock()
    plugin_manager = Mock()
    plugin_manager.get_plugin_services.return_value = [
        {
            "id": "new",
            "name": "新服务",
            "func": current_func,
            "trigger": "interval",
            "kwargs": {"minutes": 5},
            "func_kwargs": {"marker": "new"},
        }
    ]
    plugin_manager.get_plugin_attr.return_value = "测试插件"
    monkeypatch.setattr("app.scheduler.PluginManager", lambda: plugin_manager)
    backend = _FakeSchedulerBackend(["DemoPlugin_old"])
    scheduler = _build_scheduler_for_plugin_reload(
        jobs={
            "DemoPlugin_old": {
                "func": Mock(),
                "name": "旧服务",
                "pid": "DemoPlugin",
            }
        },
        backend=backend,
    )
    event = Event(EventType.PluginReload, {"plugin_id": "DemoPlugin"})

    reload_handlers = {
        item["handler_identifier"]
        for item in eventmanager.visualize_handlers()
        if item["event_type"] == EventType.PluginReload.value
        and item["status"] == "enabled"
    }
    assert "app.scheduler.Scheduler.on_plugin_reload" in reload_handlers
    scheduler.on_plugin_reload(event)
    scheduler.on_plugin_reload(event)

    assert set(scheduler._jobs) == {"DemoPlugin_new"}
    service = scheduler._jobs["DemoPlugin_new"]
    assert service["func"] is current_func
    assert service["kwargs"] == {"marker": "new"}
    assert set(backend.jobs) == {"DemoPlugin_new"}
    registered_job = backend.jobs["DemoPlugin_new"]
    assert registered_job["trigger"] == "interval"
    assert registered_job["minutes"] == 5
    assert registered_job["kwargs"] == {"job_id": "DemoPlugin_new"}
