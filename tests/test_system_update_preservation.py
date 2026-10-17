"""
检查结果与下载状态交错、旧资源暂存包恢复的回归测试。
"""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.adapters.system import update as update_module


@pytest.fixture
def manager(monkeypatch, tmp_path):
    """
    隔离更新目录、运行版本和配置，避免访问真实资源与运行状态。
    """
    monkeypatch.setattr(update_module, "get_app_version", lambda: "v3.1.4")
    monkeypatch.setattr(update_module, "get_resource_versions", lambda: ("3.0.5", "3.0.19"))
    monkeypatch.setattr(
        update_module, "get_runtime_setting",
        lambda key: tmp_path if key == "TEMP_PATH" else True,
    )
    instance = object.__new__(update_module.SystemUpdateManager)
    instance._lock = threading.RLock()
    instance._download_active = False
    instance._active_target = None
    return instance


@pytest.mark.parametrize("target", ["resources", "application"])
@pytest.mark.parametrize("state", ["downloading", "ready", "installing"])
@pytest.mark.parametrize("result", ["update", "current", "error"])
def test_check_cannot_overwrite_concurrent_download_or_install(
    monkeypatch, manager, target, state, result
):
    """
    检查请求阻塞期间发生的状态转换，在成功、无更新及失败响应后均须保留。
    """
    def response(*_args, **_kwargs):
        """
        在请求返回前模拟另一个执行者推进下载或安装。
        """
        manager._download_active = state == "downloading"
        manager._write_item(
            target, state=state, version="v3.1.5" if target == "application" else "20",
            indexer_version="3.0.20" if target == "resources" else None,
            downloaded_bytes=64, total_bytes=64,
            can_update=False, can_install=state == "ready",
        )
        if result == "error":
            raise RuntimeError("检查请求失败")
        if target == "resources":
            return {"package_version": "21", "indexer_version": "3.0.21"} if result == "update" else None
        return SimpleNamespace(
            status_code=200,
            json=lambda: [{"tag_name": "v3.1.6" if result == "update" else "v3.1.4"}],
        )

    if target == "resources":
        monkeypatch.setattr(update_module.ResourceHelper, "get_update_info", response)
    else:
        monkeypatch.setattr(manager, "_request", lambda: SimpleNamespace(get_res=response))

    status = manager.check(target)
    item = next(item for item in status.updates if item.type == target)
    assert item.state == state
    assert item.version == ("v3.1.5" if target == "application" else "20")
    assert item.downloaded_bytes == item.total_bytes == 64
    assert item.can_install is (state == "ready")
    assert item.error is None


@pytest.fixture
def prepared_resources(monkeypatch, manager):
    """
    创建当前平台完整资源包及可信摘要，复现旧检查留下的 available 状态。
    """
    names = ["user.sites.v3.bin", "sites.cpython-test.so"]
    monkeypatch.setattr(
        update_module.ResourceHelper, "_get_needed_files", classmethod(lambda cls: names),
    )
    manager._resource_dir.mkdir(parents=True)
    files = []
    for name in names:
        path = manager._resource_dir / name
        path.write_bytes(b"resource")
        files.append({"name": name, "path": str(path), "sha256": manager._sha256(path)})
    manager._merge_prepared_manifest(
        {"resource_package_version": "20", "resource_files": files, "version": "v3.1.5"},
    )
    manager._write_item(
        "resources", state="available", version="20", indexer_version="3.0.20",
        can_update=True, can_install=False,
    )
    return files


def test_recover_prepared_resources_and_install_without_downloading(
    monkeypatch, manager, prepared_resources
):
    """
    跨进程恢复后，定时及手动检查、重复下载按钮均保留制品，可继续确认安装。
    """
    request = Mock(side_effect=AssertionError("已下载资源不应重新检查或下载"))
    monkeypatch.setattr(update_module.ResourceHelper, "get_update_info", request)
    monkeypatch.setattr(manager, "_check_application", lambda: None)
    manifest = (manager._root / "prepared.json").read_bytes()
    for status in (manager.get_status(), manager.check("resources"), manager.check_scheduled(), manager.start_download("resources")):
        item = next(item for item in status.updates if item.type == "resources")
        assert item.state == "ready"
        assert item.can_install is True
        assert item.can_update is False
        assert item.downloaded_bytes == item.total_bytes == 16
        assert item.progress == 100
    assert (manager._root / "prepared.json").read_bytes() == manifest
    request.assert_not_called()
    assert len(prepared_resources) == 2
    success, _message = manager.request_install("resources")
    assert success is True
    assert json.loads(manager._install_file.read_text())["targets"] == ["resources"]


@pytest.mark.parametrize("invalid", ["missing", "modified", "incomplete", "version", "path"])
def test_unusable_prepared_resources_are_not_restored(manager, prepared_resources, invalid):
    """
    残缺、被修改、越界或版本不匹配的资源包不能恢复成可安装状态。
    """
    path = manager._resource_dir / prepared_resources[0]["name"]
    if invalid == "missing":
        path.unlink()
    elif invalid == "modified":
        path.write_bytes(b"changed")
    elif invalid == "incomplete":
        manager._merge_prepared_manifest({"resource_files": prepared_resources[:1]})
    elif invalid == "version":
        manager._merge_prepared_manifest({"resource_package_version": "19"})
    else:
        prepared_resources[0]["path"] = str(manager._root.parent / "outside.bin")
        manager._merge_prepared_manifest({"resource_files": prepared_resources})
    item = next(item for item in manager.get_status().updates if item.type == "resources")
    assert item.state == "available"
    assert item.can_install is False


def test_applied_resources_clear_recoverable_package(monkeypatch, manager, prepared_resources):
    """
    外部安装已达到目标时，应清理旧资源清单并保留主程序制品。
    """
    monkeypatch.setattr(update_module, "get_resource_versions", lambda: ("3.0.5", "3.0.20"))
    item = next(item for item in manager.get_status().updates if item.type == "resources")
    assert item.state == "idle"
    assert item.can_install is False
    # 再次读取不得把已安装的旧清单恢复成待安装。
    assert next(item for item in manager.get_status().updates if item.type == "resources").state == "idle"
    prepared = manager._read_prepared_manifest()
    assert "resource_files" not in prepared
    assert prepared["version"] == "v3.1.5"
    assert len(prepared_resources) == 2
