import os
import warnings
from pathlib import Path

from app.foundation.environment import (
    is_free_threaded_runtime,
    is_windows,
)
from app.runtime.compat.imports import install_legacy_import_hook

_windows_dll_directory_handles: list[object] = []


def _configure_free_threaded_windows_native_dependencies() -> None:
    """为 Windows free-threaded 运行时注册外部原生依赖目录。"""
    if not is_windows() or not is_free_threaded_runtime():
        return
    configured_path = os.getenv("PGBIN")
    if not configured_path:
        return
    directory = Path(configured_path)
    if not directory.is_dir():
        return
    _windows_dll_directory_handles.append(
        getattr(os, "add_dll_directory")(str(directory))
    )


def _filter_third_party_startup_warnings() -> None:
    """
    过滤第三方库在新版 Python 下产生的已知无害启动警告。
    """
    warnings.filterwarnings(
        "ignore",
        message=r"'_UnionGenericAlias' is deprecated and slated for removal in Python 3\.17",
        category=DeprecationWarning,
        module=r"google\.genai\.types",
    )
    warnings.filterwarnings(
        "ignore",
        message=r'"\\&" is an invalid escape sequence\..*',
        category=SyntaxWarning,
    )


_configure_free_threaded_windows_native_dependencies()
_filter_third_party_startup_warnings()
install_legacy_import_hook()
