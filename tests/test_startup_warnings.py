import warnings

import app


def test_app_installs_known_oss2_invalid_escape_warning_filter():
    """
    app 初始化过滤器应覆盖 oss2 的无效转义警告。
    """
    app._filter_third_party_startup_warnings()
    action, message, category, module, lineno = warnings.filters[0]

    assert action == "ignore"
    assert message.match("invalid escape sequence '\\&'")
    assert category is SyntaxWarning
    assert module is None
    assert lineno == 0


def test_app_installs_google_genai_python314_warning_filter():
    """app 初始化过滤器应覆盖 Google GenAI SDK 的 Python 3.14 弃用警告。"""
    app._filter_third_party_startup_warnings()

    assert any(
        action == "ignore"
        and message.match("'_UnionGenericAlias' is deprecated and slated for removal in Python 3.17")
        and category is DeprecationWarning
        and module is not None
        and module.match("google.genai.types")
        and lineno == 0
        for action, message, category, module, lineno in warnings.filters
    )
