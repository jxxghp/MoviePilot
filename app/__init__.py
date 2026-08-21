import warnings

from app.runtime.compat.imports import install_legacy_import_hook


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
        message=r"invalid escape sequence '\\&'",
        category=SyntaxWarning,
    )


_filter_third_party_startup_warnings()
install_legacy_import_hook()
