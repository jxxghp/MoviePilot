import os
import warnings

from app.foundation.environment import (
    is_free_threaded_runtime,
    is_windows,
    register_windows_dll_directory,
)
from app.runtime.compat.imports import install_legacy_import_hook


def _configure_free_threaded_windows_native_dependencies() -> None:
    """为 Windows free-threaded 运行时注册外部原生依赖目录。"""
    if is_windows() and is_free_threaded_runtime():
        register_windows_dll_directory(os.getenv("PGBIN"))


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
