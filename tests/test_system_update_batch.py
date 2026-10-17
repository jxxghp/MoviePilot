"""
合并更新、跨版本选择和主动刷新待安装目标的离线回归。
"""

import json
import threading
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.adapters.system import update as update_module


@pytest.fixture
def manager(monkeypatch, tmp_path):
    """
    隔离制品目录、宿主版本和资源版本，所有外部访问由用例替换。
    """
    settings = {
        "TEMP_PATH": tmp_path / "temp", "ROOT_PATH": tmp_path / "app",
        "FRONTEND_PATH": tmp_path / "public", "AUTO_UPDATE_RESOURCE": True,
        "MOVIEPILOT_AUTO_UPDATE": True,
    }
    monkeypatch.setattr(update_module, "get_runtime_setting", settings.get)
    monkeypatch.setattr(update_module, "get_app_version", lambda: "v3.0.0")
    monkeypatch.setattr(update_module, "get_resource_versions", lambda: ("1", "1"))
    monkeypatch.setattr(update_module, "is_exe", lambda: False)
    instance = object.__new__(update_module.SystemUpdateManager)
    instance._lock = threading.RLock()
    instance._download_active = False
    instance._active_target = None
    return instance


def _release(version, *, next_page=False):
    """
    构造带分页信息的 GitHub 正式版本响应。
    """
    return SimpleNamespace(
        status_code=200, json=lambda: [{"tag_name": version}],
        links={"next": {"url": "ignored"}} if next_page else {},
    )


def _available(manager):
    """
    准备两类可用更新并替换在线检查，保留真实写入及下载编排。
    """
    def application(**kwargs):
        """
        模拟主程序检查结果。
        """
        manager._write_check_result("application", state="available", version="v3.1.7", error=None, **kwargs)

    def resources(**kwargs):
        """
        模拟资源检查结果。
        """
        manager._write_check_result("resources", state="available", version="2", indexer_version="2", error=None, **kwargs)

    manager._check_application = application
    manager._check_resources = resources
    manager.check()


def _ready(manager, monkeypatch):
    """
    创建两类可校验制品，供真实合并安装意图使用。
    """
    names = ["user.sites.v3.bin", "sites.cpython-test.so"]
    monkeypatch.setattr(update_module.ResourceHelper, "_get_needed_files", classmethod(lambda cls: names))
    manager._root.mkdir(parents=True, exist_ok=True)
    for path in (manager._backend_archive, manager._frontend_archive):
        path.write_bytes(b"archive")
    manager._resource_dir.mkdir()
    files = []
    for name in names:
        path = manager._resource_dir / name
        path.write_bytes(b"new-resource")
        files.append({"name": name, "path": str(path), "sha256": manager._sha256(path)})
    manager._merge_prepared_manifest({
        "version": "v3.1.7", "frontend_version": "v3.1.6",
        "backend_archive": str(manager._backend_archive), "frontend_archive": str(manager._frontend_archive),
        "backend_sha256": manager._sha256(manager._backend_archive),
        "frontend_sha256": manager._sha256(manager._frontend_archive),
        "resource_package_version": "2", "resource_files": files,
    })
    manager._write_item("application", state="ready", version="v3.1.7", can_install=True)
    manager._write_item("resources", state="ready", version="2", indexer_version="2", can_install=True)
    return files


def test_check_selects_highest_stable_version_across_pages(monkeypatch, manager):
    """
    旧版本重发、采集器发布和预发布都不能阻止跨小版本选择最高正式版本。
    """
    responses = [
        SimpleNamespace(status_code=200, links={"next": {}}, json=lambda: [
            {"tag_name": "v3.0.10-1"}, {"tag_name": "v3.1.5"},
            {"tag_name": "v3.9.0", "draft": True}, {"tag_name": "site-adapter-collector-v1.9.0"},
            {"tag_name": "v3.2.0-beta", "prerelease": True},
        ]),
        _release("v3.1.7"),
    ]
    # GitHub next 条目只提供存在性；请求始终发往固定仓库，不能携带认证头跟随外部地址。
    responses[0].links = {"next": {"url": "https://untrusted.invalid"}}
    request = Mock(side_effect=responses)
    monkeypatch.setattr(manager, "_request", lambda: SimpleNamespace(get_res=request))
    status = manager.check("application")
    assert status.version == "v3.1.7"
    assert request.call_args_list[0].args[0].endswith("?per_page=100&page=1")
    assert request.call_args_list[1].args[0].endswith("?per_page=100&page=2")


@pytest.mark.parametrize("remote", ["v3.1.7", "v3.1.5", "v3.1.4", "error"])
def test_manual_refresh_only_replaces_ready_package_with_newer_version(monkeypatch, manager, remote):
    """
    手动检查只丢弃被新版本取代的主程序清单，失败或较旧响应保留原包及资源。
    """
    manager._merge_prepared_manifest({"version": "v3.1.5", "resource_package_version": "2"})
    manager._write_item("application", state="ready", version="v3.1.5", can_install=True)
    request = Mock(side_effect=RuntimeError("offline")) if remote == "error" else Mock(return_value=_release(remote))
    monkeypatch.setattr(manager, "_request", lambda: SimpleNamespace(get_res=request))
    status = manager.check("application", refresh_ready=True)
    assert status.state == ("available" if remote == "v3.1.7" else "ready")
    assert status.version == ("v3.1.7" if remote == "v3.1.7" else "v3.1.5")
    prepared = manager._read_prepared_manifest()
    assert prepared["resource_package_version"] == "2"
    assert ("version" in prepared) is (remote != "v3.1.7")


@pytest.mark.parametrize("state", ["downloading", "installing", "ready"])
def test_manual_refresh_preserves_a_concurrent_new_target(monkeypatch, manager, state):
    """
    主动刷新等待网络时开始的新下载、安装或新完成制品不得被旧请求覆盖。
    """
    manager._write_item("application", state="ready", version="v3.1.5", can_install=True)
    def response(_url):
        """
        模拟并发写入更高目标后，返回本次过时检查结果。
        """
        manager._download_active = state == "downloading"
        manager._write_item("application", state=state, version="v3.1.8")
        return _release("v3.1.7")
    monkeypatch.setattr(manager, "_request", lambda: SimpleNamespace(get_res=response))
    status = manager.check("application", refresh_ready=True)
    assert (status.state, status.version) == (state, "v3.1.8")


def test_manual_refresh_replaces_resource_target_and_preserves_application(monkeypatch, manager):
    """
    资源包主动刷新也可直接跳过旧版本，并保留已准备的主程序制品。
    """
    _ready(manager, monkeypatch)
    monkeypatch.setattr(update_module.ResourceHelper, "get_update_info", lambda self, **kwargs: {
        "package_version": "5", "auth_version": "3", "indexer_version": "5",
    })
    status = manager.check("resources", refresh_ready=True)
    resource = next(item for item in status.updates if item.type == "resources")
    assert (resource.state, resource.version) == ("available", "5")
    assert manager._read_prepared_manifest()["version"] == "v3.1.7"
    assert "resource_files" not in manager._read_prepared_manifest()


def test_install_state_write_failure_does_not_commit_install_intent(monkeypatch, manager):
    """
    状态写入失败时不得留下重启安装意图，另一类更新仍可保留待安装包。
    """
    _ready(manager, monkeypatch)
    persist = manager._persist_state
    write = Mock(side_effect=[OSError("disk failure"), None, None])
    def persist_once(state):
        """
        仅首次安装状态写入失败，后续状态恢复使用真实持久化。
        """
        write(state)
        return persist(state)
    monkeypatch.setattr(manager, "_persist_state", persist_once)
    success, message = manager.request_install("all")
    assert not success and "disk failure" in message
    assert not manager._install_file.exists()
    assert manager.get_status().updates[0].state == "ready"
    assert manager.get_status().updates[1].state == "failed"


def test_batch_download_uses_one_worker_and_prevents_early_install(monkeypatch, manager):
    """
    主程序准备完成后必须继续等待资源，同一批只有一个后台任务且不能提前重启。
    """
    _available(manager)
    submit = Mock()
    monkeypatch.setattr(update_module, "ThreadHelper", lambda: SimpleNamespace(submit=submit))
    visited = []
    def application():
        """
        模拟主程序先完成，检查资源尚未完成时的安装边界。
        """
        assert manager._active_target == "application"
        visited.append("application")
        manager._write_item("application", state="ready", can_install=True)
        assert manager.request_install("all")[0] is False
        assert manager.request_install("application")[0] is False
        assert not manager._install_file.exists()
    def resources():
        """
        模拟第二个资源任务完成。
        """
        assert manager._active_target == "resources"
        visited.append("resources")
        manager._write_item("resources", state="ready", can_install=True)
    monkeypatch.setattr(manager, "_download_application", application)
    monkeypatch.setattr(manager, "_download_resources", resources)
    status = manager.start_download("all")
    assert all(item.state == "downloading" for item in status.updates)
    manager.start_download("all")
    submit.assert_called_once()
    callback, target = submit.call_args.args
    callback(target)
    assert visited == ["application", "resources"]
    assert all(item.state == "ready" for item in manager.get_status().updates)
    assert manager._download_active is False


def test_retry_batch_download_reuses_successful_application(monkeypatch, manager):
    """
    资源失败后合并重试只补资源下载，不重复准备仍为最新的主程序。
    """
    _available(manager)
    monkeypatch.setattr(update_module, "ThreadHelper", lambda: SimpleNamespace(submit=lambda function, target: function(target)))
    application = Mock(side_effect=lambda: manager._write_item("application", state="ready", can_install=True))
    resources = Mock(side_effect=RuntimeError("resource download failed"))
    monkeypatch.setattr(manager, "_download_application", application)
    monkeypatch.setattr(manager, "_download_resources", resources)
    status = manager.start_download("all")
    assert [item.state for item in status.updates] == ["ready", "failed"]
    assert manager.request_install("all")[0] is False
    resources.side_effect = lambda: manager._write_item("resources", state="ready", can_install=True)
    status = manager.start_download("all")
    assert all(item.state == "ready" for item in status.updates)
    application.assert_called_once()
    assert resources.call_count == 2


def test_combined_install_writes_one_manifest_and_marks_both_installing(monkeypatch, manager):
    """
    两个包全部校验后只写一份安装意图，重启失败可一起撤销回到 ready。
    """
    _ready(manager, monkeypatch)
    assert manager.request_install("all")[0] is True
    assert json.loads(manager._install_file.read_text())["targets"] == ["application", "resources"]
    assert all(item.state == "installing" for item in manager.get_status().updates)
    manager.cancel_install("restart failed")
    assert not manager._install_file.exists()
    assert all(item.state == "ready" for item in manager.get_status().updates)


def test_combined_install_rejects_corrupt_resource_without_partial_intent(monkeypatch, manager):
    """
    任一制品校验失败时不留下部分安装请求，另一类完整制品继续可用。
    """
    files = _ready(manager, monkeypatch)
    manager._resource_dir.joinpath(files[0]["name"]).write_bytes(b"corrupt")
    assert manager.request_install("all")[0] is False
    assert not manager._install_file.exists()
    assert manager.get_status().updates[0].state == "ready"
    assert manager.get_status().updates[1].state == "failed"


def test_combined_docker_install_applies_all_payloads_in_one_transaction(monkeypatch, manager):
    """
    真实目录事务在一次消费中替换前后端及资源，同时保留插件并清理两类意图。
    """
    _ready(manager, monkeypatch)
    app = manager._docker_app_dir
    public = manager._docker_public_dir
    resource = app / "app/application/site"
    plugin = app / "app/plugins/example"
    resource.mkdir(parents=True)
    plugin.mkdir(parents=True)
    public.mkdir()
    (plugin / "__init__.py").write_text("custom plugin")
    (resource / "user.sites.v3.bin").write_bytes(b"old-resource")
    for name in ("pyproject.toml", "uv.lock"):
        (app / name).write_text("same")
    with zipfile.ZipFile(manager._backend_archive, "w") as archive:
        archive.writestr("repo/version.py", "APP_VERSION = 'v3.1.7'\nFRONTEND_VERSION = 'v3.1.6'\n")
        archive.writestr("repo/app/plugins/__init__.py", "new package")
        archive.writestr("repo/app/application/site/auth.py", "new source")
        for name in ("pyproject.toml", "uv.lock"):
            archive.writestr(f"repo/{name}", "same")
    with zipfile.ZipFile(manager._frontend_archive, "w") as archive:
        archive.writestr("dist/index.html", "new frontend")
        archive.writestr("dist/version.txt", "v3.1.6")
    manager._merge_prepared_manifest({
        "backend_sha256": manager._sha256(manager._backend_archive),
        "frontend_sha256": manager._sha256(manager._frontend_archive),
    })
    monkeypatch.setattr(update_module, "is_docker", lambda: True)
    assert manager.request_install("all")[0] is True
    assert manager.apply_prepared_update()[0] is True
    assert (app / "version.py").read_text().startswith("APP_VERSION = 'v3.1.7'")
    assert (public / "index.html").read_text() == "new frontend"
    assert (resource / "user.sites.v3.bin").read_bytes() == b"new-resource"
    assert (resource / "sites.cpython-test.so").read_bytes() == b"new-resource"
    assert (plugin / "__init__.py").read_text() == "custom plugin"
    assert not manager._install_file.exists()
    assert not (manager._root / "prepared.json").exists()
