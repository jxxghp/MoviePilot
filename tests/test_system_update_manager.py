"""系统后台更新状态机测试。"""

import json
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.system import update as update_module


def _manager(monkeypatch, tmp_path: Path):
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


def _response(payload, status_code=200):
    return SimpleNamespace(status_code=status_code, json=lambda: payload)


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
