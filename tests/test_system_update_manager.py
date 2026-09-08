"""系统后台更新状态机测试。"""

import errno
import json
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.system import update as update_module


def _manager(monkeypatch, tmp_path: Path):
    """创建使用临时状态目录的更新管理器。"""
    monkeypatch.setattr(
        update_module,
        "get_runtime_setting",
        lambda key: tmp_path if key == "TEMP_PATH" else None,
    )
    manager = object.__new__(update_module.SystemUpdateManager)
    manager._lock = threading.RLock()
    manager._download_active = False
    manager._active_target = None
    return manager


def _docker_manager(monkeypatch, tmp_path: Path):
    """创建指向隔离 Docker 目录的更新管理器。"""
    runtime_settings = {
        "TEMP_PATH": tmp_path / "config" / "temp",
        "ROOT_PATH": tmp_path / "app",
        "FRONTEND_PATH": tmp_path / "public",
        "VENV_PATH": tmp_path / "venv",
        "UV_BIN": tmp_path / "uv",
        "MOVIEPILOT_AUTO_UPDATE": False,
        "AUTO_UPDATE_RESOURCE": True,
        "PIP_PROXY": "",
        "PROXY_HOST": "",
    }
    monkeypatch.setattr(
        update_module,
        "get_runtime_setting",
        lambda key: runtime_settings[key],
    )
    monkeypatch.setattr(update_module, "is_docker", lambda: True)
    manager = object.__new__(update_module.SystemUpdateManager)
    manager._lock = threading.RLock()
    manager._download_active = False
    manager._active_target = None
    return manager


def _response(payload, status_code=200):
    return SimpleNamespace(status_code=status_code, json=lambda: payload)


def test_status_reads_live_auto_update_setting_without_discarding_cached_update(monkeypatch, tmp_path):
    """切换提醒设置立即反映到状态，缓存版本仍供手动升级使用。"""
    manager = _manager(monkeypatch, tmp_path)
    manager._write_state(state="available", version="v3.1.0", can_update=True)
    for enabled in (True, False, True):
        monkeypatch.setattr(
            update_module, "get_runtime_setting",
            lambda key: tmp_path if key == "TEMP_PATH" else enabled,
        )
        status = manager.get_status()
        assert status.auto_update is enabled
        assert status.state == "available"
        assert status.version == "v3.1.0"
        assert status.can_update is True


@pytest.mark.parametrize("auto_update", [False, True])
@pytest.mark.parametrize("auto_update_resource", [False, True])
def test_scheduled_check_respects_independent_switches(
    monkeypatch, tmp_path, auto_update, auto_update_resource
):
    """自动检查只访问已开启的目标；手动检查仍可访问两类更新。"""
    manager = _manager(monkeypatch, tmp_path)
    values = {
        "TEMP_PATH": tmp_path,
        "MOVIEPILOT_AUTO_UPDATE": auto_update,
        "AUTO_UPDATE_RESOURCE": auto_update_resource,
    }
    monkeypatch.setattr(update_module, "get_runtime_setting", values.get)
    checked = []
    monkeypatch.setattr(manager, "_check_application", lambda: checked.append("application"))
    monkeypatch.setattr(manager, "_check_resources", lambda: checked.append("resources"))

    status = manager.check_scheduled()
    assert checked == [
        target for target, enabled in (("application", auto_update), ("resources", auto_update_resource))
        if enabled
    ]
    assert status.auto_update is auto_update
    assert status.auto_update_resource is auto_update_resource

    checked.clear()
    manager.check()
    assert checked == ["application", "resources"]


def test_check_exposes_new_stable_release(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path)
    logs = []
    releases = [
        {"tag_name": "v3.2.0-beta", "prerelease": True, "draft": False},
        {
            "tag_name": "v3.1.0",
            "name": "MoviePilot v3.1.0",
            "body": "changes",
            "published_at": "2026-08-24T00:00:00Z",
            "prerelease": False,
            "draft": False,
        },
    ]
    monkeypatch.setattr(manager, "_request", lambda: SimpleNamespace(get_res=lambda _url: _response(releases)))
    monkeypatch.setattr(update_module, "get_app_version", lambda: "v3.0.0")
    monkeypatch.setattr(update_module.logger, "info", logs.append)

    status = manager.check("application")

    assert status.state == "available"
    assert status.version == "v3.1.0"
    assert status.release_notes == "changes"
    assert status.can_update is True
    assert logs == ["发现 MoviePilot 主程序更新：v3.0.0 -> v3.1.0"]


def test_check_logs_when_application_is_current(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path)
    logs = []
    releases = [
        {
            "tag_name": "v3.0.0",
            "name": "MoviePilot v3.0.0",
            "prerelease": False,
            "draft": False,
        }
    ]
    monkeypatch.setattr(manager, "_request", lambda: SimpleNamespace(get_res=lambda _url: _response(releases)))
    monkeypatch.setattr(update_module, "get_app_version", lambda: "v3.0.0")
    monkeypatch.setattr(update_module.logger, "info", logs.append)

    status = manager.check("application")

    assert status.state == "idle"
    assert status.can_update is False
    assert logs == ["MoviePilot 主程序已是最新版本：v3.0.0"]


def test_scheduled_check_failure_stays_silent(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        manager,
        "_request",
        lambda: SimpleNamespace(get_res=lambda _url: _response({}, status_code=503)),
    )

    status = manager.check("application")

    assert status.state == "idle"
    assert status.error


def test_interrupted_download_becomes_retryable_failure(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path)
    manager._write_state(state="downloading", version="v3.1.0")

    status = manager.get_status()

    assert status.state == "failed"
    assert status.can_update is True
    assert "中断" in status.error


def test_ready_resource_update_clears_after_loaded_version_reaches_target(
    monkeypatch,
    tmp_path,
):
    """运行资源已达到目标版本时应清除残留提示，并保留另一类待安装制品。"""
    versions = ["3.0.1", "3.0.9"]
    monkeypatch.setattr(
        update_module,
        "get_resource_versions",
        lambda: tuple(versions),
    )
    manager = _manager(monkeypatch, tmp_path)
    manager._merge_prepared_manifest(
        {
            "version": "v3.1.0",
            "backend_archive": "/tmp/backend.zip",
            "resource_package_version": "10",
            "resource_files": [{"name": "user.sites.v3.bin"}],
        }
    )
    manager._write_item(
        "resources",
        state="ready",
        version="10",
        current_auth_version="3.0.1",
        current_indexer_version="3.0.9",
        indexer_version="3.0.10",
        can_update=False,
        can_install=True,
    )

    pending = manager.get_status()
    pending_resource = next(item for item in pending.updates if item.type == "resources")
    assert pending_resource.state == "ready"

    versions[1] = "3.0.10"
    current = manager.get_status()
    current_resource = next(item for item in current.updates if item.type == "resources")
    prepared = json.loads((manager._root / "prepared.json").read_text(encoding="utf-8"))

    assert current_resource.state == "idle"
    assert current_resource.current_indexer_version == "3.0.10"
    assert current_resource.indexer_version is None
    assert current_resource.version is None
    assert current_resource.can_install is False
    assert prepared["version"] == "v3.1.0"
    assert prepared["backend_archive"] == "/tmp/backend.zip"
    assert "resource_package_version" not in prepared
    assert "resource_files" not in prepared


def test_download_prepares_matching_backend_and_frontend_archives(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path)
    backend_fixture = tmp_path / "source-backend.zip"
    frontend_fixture = tmp_path / "source-frontend.zip"
    with zipfile.ZipFile(backend_fixture, "w") as archive:
        archive.writestr(
            "MoviePilot-v3.1.0/version.py",
            "APP_VERSION = 'v3.1.0'\nFRONTEND_VERSION = 'v3.1.0'\n",
        )
        archive.writestr("MoviePilot-v3.1.0/pyproject.toml", "[project]\n")
        archive.writestr("MoviePilot-v3.1.0/uv.lock", "version = 1\n")
    with zipfile.ZipFile(frontend_fixture, "w") as archive:
        archive.writestr("dist/index.html", "ok")
        archive.writestr("dist/version.txt", "v3.1.0\n")

    fixtures = iter((backend_fixture, frontend_fixture))

    def download(_url, destination, downloaded_before, _total_hint):
        source = next(fixtures)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        size = destination.stat().st_size
        return downloaded_before + size, size

    monkeypatch.setattr(manager, "_download_file", download)
    monkeypatch.setattr(update_module, "is_docker", lambda: True)
    monkeypatch.setattr(
        manager,
        "_fetch_frontend_release",
        lambda _version: {
            "assets": [
                {
                    "name": "dist.zip",
                    "size": frontend_fixture.stat().st_size,
                    "browser_download_url": "https://example.invalid/dist.zip",
                    "digest": f"sha256:{manager._sha256(frontend_fixture)}",
                }
            ]
        },
    )

    manager._write_state(state="downloading", version="v3.1.0")
    manager._active_target = "application"
    manager._download_update("application")

    status = manager.get_status()
    prepared = json.loads((manager._root / "prepared.json").read_text(encoding="utf-8"))
    assert status.state == "ready"
    assert status.progress == 100
    assert status.frontend_version == "v3.1.0"
    assert prepared["backend_sha256"] == manager._sha256(manager._backend_archive)
    assert prepared["frontend_sha256"] == manager._sha256(manager._frontend_archive)


def test_application_download_keeps_prepared_resource_package(monkeypatch, tmp_path):
    """应用包重新下载时不得删除另一类已经准备好的资源包。"""
    manager = _manager(monkeypatch, tmp_path)
    resource_dir = manager._resource_dir
    resource_dir.mkdir(parents=True)
    resource_file = resource_dir / "user.sites.v3.bin"
    resource_file.write_bytes(b"resource")
    manager._merge_prepared_manifest(
        {
            "resource_package_version": "10",
            "resource_files": [
                {
                    "name": resource_file.name,
                    "type": "indexer",
                    "version": "3.0.8",
                    "path": str(resource_file),
                    "sha256": manager._sha256(resource_file),
                }
            ],
        }
    )
    backend_fixture = tmp_path / "source-backend.zip"
    frontend_fixture = tmp_path / "source-frontend.zip"
    with zipfile.ZipFile(backend_fixture, "w") as archive:
        archive.writestr(
            "MoviePilot-v3.1.0/version.py",
            "APP_VERSION = 'v3.1.0'\nFRONTEND_VERSION = 'v3.1.0'\n",
        )
        archive.writestr("MoviePilot-v3.1.0/pyproject.toml", "[project]\n")
        archive.writestr("MoviePilot-v3.1.0/uv.lock", "version = 1\n")
    with zipfile.ZipFile(frontend_fixture, "w") as archive:
        archive.writestr("dist/index.html", "ok")
        archive.writestr("dist/version.txt", "v3.1.0\n")

    fixtures = iter((backend_fixture, frontend_fixture))

    def download(_url, destination, downloaded_before, _total_hint):
        source = next(fixtures)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        size = destination.stat().st_size
        return downloaded_before + size, size

    monkeypatch.setattr(manager, "_download_file", download)
    monkeypatch.setattr(update_module, "is_docker", lambda: True)
    monkeypatch.setattr(
        manager,
        "_fetch_frontend_release",
        lambda _version: {
            "assets": [
                {
                    "name": "dist.zip",
                    "size": frontend_fixture.stat().st_size,
                    "browser_download_url": "https://example.invalid/dist.zip",
                }
            ]
        },
    )
    manager._write_state(state="downloading", version="v3.1.0")
    manager._active_target = "application"

    manager._download_update("application")

    prepared = json.loads((manager._root / "prepared.json").read_text(encoding="utf-8"))
    assert prepared["resource_package_version"] == "10"
    assert prepared["resource_files"][0]["name"] == "user.sites.v3.bin"


def test_resource_manifest_requires_complete_current_platform_package(monkeypatch, tmp_path):
    """资源安装只能接受同时包含索引和当前平台认证文件的完整包。"""
    manager = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        update_module.ResourceHelper,
        "_get_needed_files",
        classmethod(lambda cls: ["user.sites.v3.bin", "sites.cpython-test.so"]),
    )
    resource_dir = manager._resource_dir
    resource_dir.mkdir(parents=True)
    resource_file = resource_dir / "user.sites.v3.bin"
    resource_file.write_bytes(b"resource")
    prepared = {
        "resource_files": [
            {
                "name": resource_file.name,
                "path": str(resource_file),
                "sha256": manager._sha256(resource_file),
            }
        ]
    }

    with pytest.raises(RuntimeError, match="完整资源包"):
        manager._validate_resource_manifest(prepared)


def test_request_install_rejects_modified_prepared_package(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path)
    manager._root.mkdir(parents=True)
    manager._backend_archive.write_bytes(b"backend")
    manager._frontend_archive.write_bytes(b"frontend")
    (manager._root / "prepared.json").write_text(
        json.dumps(
            {
                "version": "v3.1.0",
                "frontend_version": "v3.1.0",
                "backend_archive": str(manager._backend_archive),
                "frontend_archive": str(manager._frontend_archive),
                "backend_sha256": "invalid",
                "frontend_sha256": manager._sha256(manager._frontend_archive),
            }
        ),
        encoding="utf-8",
    )
    manager._write_state(state="ready", version="v3.1.0", can_install=True)

    success, message = manager.request_install()

    assert success is False
    assert "后端更新包校验失败" in message
    assert not manager._install_file.exists()
    assert manager.get_status().state == "failed"


def test_cancel_install_returns_prepared_update_to_ready(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path)
    manager._root.mkdir(parents=True)
    manager._install_file.write_text("{}", encoding="utf-8")
    manager._write_state(state="installing", version="v3.1.0")

    manager.cancel_install("restart failed")

    status = manager.get_status()
    assert status.state == "ready"
    assert status.can_install is True
    assert status.error == "restart failed"
    assert not manager._install_file.exists()


def test_apply_prepared_application_replaces_docker_payload_and_preserves_plugins(
    monkeypatch, tmp_path
):
    """Docker root worker 应替换前后端目录，同时保留运行时插件和站点资源。"""
    manager = _docker_manager(monkeypatch, tmp_path)
    app_dir = manager._docker_app_dir
    public_dir = manager._docker_public_dir
    plugin_dir = app_dir / "app" / "plugins"
    resource_dir = app_dir / "app" / "application" / "site"
    plugin_dir.mkdir(parents=True)
    resource_dir.mkdir(parents=True)
    public_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("# compatibility\n", encoding="utf-8")
    (plugin_dir / "local_plugin.py").write_text("local\n", encoding="utf-8")
    (resource_dir / "user.sites.v3.bin").write_text("old-resource\n", encoding="utf-8")
    (app_dir / "old.py").write_text("old\n", encoding="utf-8")
    (app_dir / "pyproject.toml").write_text("old-project\n", encoding="utf-8")
    (app_dir / "uv.lock").write_text("old-lock\n", encoding="utf-8")
    (public_dir / "index.html").write_text("old-front\n", encoding="utf-8")
    manager._root.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(manager._backend_archive, "w") as archive:
        archive.writestr(
            "MoviePilot-v3.1.0/version.py",
            "APP_VERSION = 'v3.1.0'\nFRONTEND_VERSION = 'v3.1.0'\n",
        )
        archive.writestr("MoviePilot-v3.1.0/pyproject.toml", "[project]\n")
        archive.writestr("MoviePilot-v3.1.0/uv.lock", "version = 1\n")
        archive.writestr("MoviePilot-v3.1.0/new.py", "new\n")
    with zipfile.ZipFile(manager._frontend_archive, "w") as archive:
        archive.writestr("dist/index.html", "new-front\n")
        archive.writestr("dist/version.txt", "v3.1.0\n")

    prepared = {
        "targets": ["application"],
        "version": "v3.1.0",
        "frontend_version": "v3.1.0",
        "backend_archive": str(manager._backend_archive),
        "frontend_archive": str(manager._frontend_archive),
        "backend_sha256": manager._sha256(manager._backend_archive),
        "frontend_sha256": manager._sha256(manager._frontend_archive),
    }
    (manager._root / "prepared.json").write_text(
        json.dumps(prepared), encoding="utf-8"
    )
    manager._install_file.write_text(json.dumps(prepared), encoding="utf-8")
    sync_calls = []
    monkeypatch.setattr(
        manager,
        "_sync_docker_dependencies",
        lambda project_dir, **kwargs: sync_calls.append((project_dir, kwargs)),
    )

    success, message = manager.apply_prepared_update()

    assert success is True
    assert message == "已下载的更新已替换到 Docker 程序目录"
    assert len(sync_calls) == 1
    assert sync_calls[0][0].name == "App"
    assert sync_calls[0][1] == {}
    assert (app_dir / "new.py").read_text(encoding="utf-8") == "new\n"
    assert not (app_dir / "old.py").exists()
    assert (app_dir / "app" / "plugins" / "local_plugin.py").exists()
    assert (resource_dir / "user.sites.v3.bin").read_text(encoding="utf-8") == "old-resource\n"
    assert (public_dir / "index.html").read_text(encoding="utf-8") == "new-front\n"
    assert not manager._install_file.exists()
    assert not (manager._root / "prepared.json").exists()
    assert not manager._docker_pending_file.exists()
    assert not manager._docker_previous_app_dir.exists()
    assert not manager._docker_previous_public_dir.exists()


@pytest.mark.parametrize("failure", [None, "backup", "install"])
def test_apply_prepared_resources_replaces_complete_docker_resource_package(
    monkeypatch, tmp_path, failure
):
    """镜像层目录禁止重命名时仍能更新，备份或安装失败则保留旧资源。"""
    manager = _docker_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        update_module.ResourceHelper,
        "_get_needed_files",
        classmethod(lambda cls: ["user.sites.v3.bin", "sites.cpython-test.so"]),
    )
    monkeypatch.setattr(update_module, "get_resource_versions", lambda: ("1", "1"))
    resource_dir = manager._docker_app_dir / "app" / "application" / "site"
    resource_dir.mkdir(parents=True)
    (manager._docker_app_dir / "keep.py").parent.mkdir(parents=True, exist_ok=True)
    (manager._docker_app_dir / "keep.py").write_text("keep\n", encoding="utf-8")
    (resource_dir / "user.sites.v3.bin").write_bytes(b"old-index")
    (resource_dir / "sites.cpython-old.so").write_bytes(b"old-native")
    prepared_files = []
    for name, content in (
        ("user.sites.v3.bin", b"new-index"),
        ("sites.cpython-test.so", b"new-native"),
    ):
        path = manager._resource_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        prepared_files.append(
            {"name": name, "path": str(path), "sha256": manager._sha256(path)}
        )
    prepared = {
        "targets": ["resources"],
        "resource_package_version": "10",
        "resource_files": prepared_files,
    }
    manager._root.mkdir(parents=True, exist_ok=True)
    (manager._root / "prepared.json").write_text(
        json.dumps(prepared), encoding="utf-8"
    )
    manager._install_file.write_text(json.dumps(prepared), encoding="utf-8")

    original_replace = Path.replace
    original_copytree = update_module.shutil.copytree

    def replace(source, target):
        """模拟 OverlayFS 镜像层重命名限制及新资源提交失败。"""
        if source == resource_dir:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        if failure == "install" and source.parent.name.startswith(".moviepilot-resource-update-"):
            raise OSError(errno.EIO, "install failed")
        return original_replace(source, target)

    def copytree(source, target, *args, **kwargs):
        """模拟备份复制失败，确保尚未触碰运行目录。"""
        if failure == "backup" and Path(target).name.endswith(".__prepared_previous__"):
            raise OSError(errno.ENOSPC, "backup failed")
        return original_copytree(source, target, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(update_module.shutil, "copytree", copytree)
    success, _message = manager.apply_prepared_update()

    if failure:
        assert success is False
        assert (resource_dir / "user.sites.v3.bin").read_bytes() == b"old-index"
        assert (resource_dir / "sites.cpython-old.so").read_bytes() == b"old-native"
        assert not (resource_dir / "sites.cpython-test.so").exists()
        assert (manager._root / "prepared.json").exists()
        return
    assert success is True
    assert (manager._docker_app_dir / "keep.py").read_text(encoding="utf-8") == "keep\n"
    assert (resource_dir / "user.sites.v3.bin").read_bytes() == b"new-index"
    assert (resource_dir / "sites.cpython-test.so").read_bytes() == b"new-native"
    assert not (resource_dir / "sites.cpython-old.so").exists()
    assert not manager._install_file.exists()
    assert not (manager._root / "prepared.json").exists()
