"""存储后端升为一级模块后，插件旧导入路径必须解析到同一对象。"""

import importlib

import pytest

# 旧存储实现模块路径与迁移后的 canonical 路径
LEGACY_STORAGE_MODULES = (
    ("app.modules.filemanager.storages.alipan", "app.modules.alipan.alipan", "AliPan"),
    ("app.modules.filemanager.storages.alist", "app.modules.alist.alist", "Alist"),
    ("app.modules.filemanager.storages.alistgo", "app.modules.alistgo.alistgo", "AlistGo"),
    ("app.modules.filemanager.storages.local", "app.modules.localstorage.local", "LocalStorage"),
    ("app.modules.filemanager.storages.rclone", "app.modules.rclone.rclone", "Rclone"),
    ("app.modules.filemanager.storages.smb", "app.modules.smb.smb", "SMB"),
    ("app.modules.filemanager.storages.u115", "app.modules.u115.u115", "U115Pan"),
)

# 旧存储包公开的符号
LEGACY_PACKAGE_SYMBOLS = ("StorageBase", "transfer_process")


@pytest.mark.parametrize("legacy_name,canonical_name,symbol", LEGACY_STORAGE_MODULES)
def test_legacy_storage_module_resolves_to_canonical_object(legacy_name, canonical_name, symbol):
    """旧存储实现路径与新位置必须是同一个模块对象和同一个类。"""
    legacy = importlib.import_module(legacy_name)
    canonical = importlib.import_module(canonical_name)

    assert legacy is canonical
    assert getattr(legacy, symbol) is getattr(canonical, symbol)


@pytest.mark.parametrize("symbol", LEGACY_PACKAGE_SYMBOLS)
def test_legacy_storage_package_symbols_resolve_to_canonical(symbol):
    """旧存储包公开的存储基类与传输进度回调解析到模块样板基类包。"""
    legacy = importlib.import_module("app.modules.filemanager.storages")
    canonical = importlib.import_module("app.modules._base.storage")

    assert getattr(legacy, symbol) is getattr(canonical, symbol)


def test_legacy_package_resolves_to_media_library_module_object():
    """旧文件管理包路径与媒体库文件系统包必须是同一个模块对象。"""
    legacy = importlib.import_module("app.modules.filemanager")
    canonical = importlib.import_module("app.modules.medialibrary")

    assert legacy is canonical


def test_legacy_capability_class_name_resolves_to_canonical_class():
    """插件按旧类名反射能力类时必须拿到 canonical 类本身。"""
    legacy = importlib.import_module("app.modules.filemanager")
    canonical = importlib.import_module("app.modules.medialibrary")

    assert legacy.FileManagerModule is canonical.MediaLibraryModule


def test_legacy_package_still_exports_storage_base():
    """插件历史上从文件管理包直接取用存储基类，该导出必须保持同一对象。"""
    legacy = importlib.import_module("app.modules.filemanager")
    canonical = importlib.import_module("app.modules._base.storage")

    assert legacy.StorageBase is canonical.StorageBase


def test_legacy_package_still_exports_transfer_handler():
    """插件历史上从文件管理包直接取用整理编排类，该导出必须保持同一对象。"""
    legacy = importlib.import_module("app.modules.filemanager")
    canonical = importlib.import_module("app.application.transferhandler")

    assert legacy.TransHandler is canonical.TransHandler


def test_monkey_patching_legacy_path_reaches_canonical_class():
    """存量插件按旧路径 monkey-patch 存储实现，必须改到 canonical 类上。"""
    legacy = importlib.import_module("app.modules.filemanager.storages.u115")
    canonical = importlib.import_module("app.modules.u115.u115")
    sentinel = object()

    legacy.U115Pan._compat_probe = sentinel
    try:
        assert canonical.U115Pan._compat_probe is sentinel
    finally:
        del canonical.U115Pan._compat_probe


def test_removed_storage_catalog_path_is_blocked():
    """已删除的内建存储清单模块不得再从旧路径解析出来。"""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.modules.filemanager.storages.catalog")
