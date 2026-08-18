"""内建存储后端的登记清单。"""

import importlib

from app.runtime.extensions.storage_registry import storage_backend_registry
from app.runtime.log import logger

# 内建存储后端：模块路径与类名
BUILTIN_STORAGE_BACKENDS = (
    ("app.modules.filemanager.storages.alipan", "AliPan"),
    ("app.modules.filemanager.storages.alist", "Alist"),
    ("app.modules.filemanager.storages.alistgo", "AlistGo"),
    ("app.modules.filemanager.storages.local", "LocalStorage"),
    ("app.modules.filemanager.storages.rclone", "Rclone"),
    ("app.modules.filemanager.storages.smb", "SMB"),
    ("app.modules.filemanager.storages.u115", "U115Pan"),
)


def register_builtin_storage_backends() -> None:
    """把内建存储后端登记到存储后端注册表，单个后端不可用不影响其余后端。"""
    for module_path, class_name in BUILTIN_STORAGE_BACKENDS:
        try:
            backend = getattr(importlib.import_module(module_path), class_name)
        except Exception as err:
            logger.error(f"【存储】加载存储后端 {class_name} 出错：{str(err)}")
            continue
        storage_backend_registry.register(backend)
