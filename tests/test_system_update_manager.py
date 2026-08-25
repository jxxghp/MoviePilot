"""系统后台更新状态机测试。"""

import json
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

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
    return manager


def _response(payload, status_code=200):
    return SimpleNamespace(status_code=status_code, json=lambda: payload)


def test_check_exposes_new_stable_release(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path)
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

    status = manager.check()

    assert status.state == "available"
    assert status.version == "v3.1.0"
    assert status.release_notes == "changes"
    assert status.can_update is True


def test_scheduled_check_failure_stays_silent(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        manager,
        "_request",
        lambda: SimpleNamespace(get_res=lambda _url: _response({}, status_code=503)),
    )

    status = manager.check()

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
    manager._download_update("v3.1.0")

    status = manager.get_status()
    prepared = json.loads((manager._root / "prepared.json").read_text(encoding="utf-8"))
    assert status.state == "ready"
    assert status.progress == 100
    assert status.frontend_version == "v3.1.0"
    assert prepared["backend_sha256"] == manager._sha256(manager._backend_archive)
    assert prepared["frontend_sha256"] == manager._sha256(manager._frontend_archive)


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
