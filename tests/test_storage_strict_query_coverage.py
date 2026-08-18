import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules._base.storage import StorageBase
from app.schemas.exception import StorageQueryError

# 在树存储实现模块，全部必须实现严格查询
BUILTIN_STORAGE_MODULES = [
    "app.modules.alipan.alipan",
    "app.modules.alist.alist",
    "app.modules.localstorage.local",
    "app.modules.rclone.rclone",
    "app.modules.smb.smb",
    "app.modules.u115.u115",
]


def test_base_get_item_strict_fails_conservatively():
    """未覆写严格查询的存储必须保守失败，而不是回退到宽松查询。"""
    stub = SimpleNamespace(schema="dummy")

    with pytest.raises(StorageQueryError):
        StorageBase.get_item_strict(stub, Path("/media/示例.mkv"))


@pytest.mark.parametrize("module_name", BUILTIN_STORAGE_MODULES)
def test_builtin_storage_overrides_strict_query(module_name):
    """每个在树存储都必须覆写严格查询，否则整理会被基类保守拒绝。"""
    module = importlib.import_module(module_name)
    storage_classes = [
        obj for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, StorageBase)
        and obj is not StorageBase
        and obj.__module__ == module.__name__
    ]

    assert storage_classes, f"{module_name} 未定义存储类"
    for storage_class in storage_classes:
        assert "get_item_strict" in vars(storage_class), \
            f"{storage_class.__name__} 未实现 get_item_strict"
