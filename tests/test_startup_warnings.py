import warnings

import app


def test_app_installs_known_oss2_invalid_escape_warning_filter():
    """
    app 初始化过滤器应覆盖 oss2 的无效转义警告。
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        app._filter_third_party_startup_warnings()
        warnings.warn("invalid escape sequence '\\&'", SyntaxWarning)

    assert caught == []


def test_app_installs_google_genai_python314_warning_filter():
    """app 初始化过滤器应覆盖 Google GenAI SDK 的 Python 3.14 弃用警告。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        app._filter_third_party_startup_warnings()
        warnings.warn_explicit(
            "'_UnionGenericAlias' is deprecated and slated for removal in Python 3.17",
            DeprecationWarning,
            filename="google/genai/types.py",
            lineno=1,
            module="google.genai.types",
        )

    assert caught == []
