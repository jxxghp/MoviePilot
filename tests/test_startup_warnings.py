import warnings

import pytest

import app


def test_app_registers_pg_bin_for_windows_free_threaded(monkeypatch):
    """Windows free-threaded 启动时应注册 PostgreSQL DLL 目录。"""
    registered = []
    monkeypatch.setattr(app, "is_windows", lambda: True)
    monkeypatch.setattr(app, "is_free_threaded_runtime", lambda: True)
    monkeypatch.setattr(
        app,
        "register_windows_dll_directory",
        registered.append,
    )
    monkeypatch.setenv("PGBIN", "C:/PostgreSQL/bin")

    app._configure_free_threaded_windows_native_dependencies()

    assert registered == ["C:/PostgreSQL/bin"]


def test_app_skips_pg_bin_for_standard_runtime(monkeypatch):
    """标准 Windows 运行时不应执行 free-threaded 专属 DLL 注册。"""
    monkeypatch.setattr(app, "is_windows", lambda: True)
    monkeypatch.setattr(app, "is_free_threaded_runtime", lambda: False)
    monkeypatch.setattr(
        app,
        "register_windows_dll_directory",
        lambda _path: pytest.fail("must not register a DLL directory"),
    )
    monkeypatch.setenv("PGBIN", "C:/PostgreSQL/bin")

    app._configure_free_threaded_windows_native_dependencies()


def test_app_installs_known_oss2_invalid_escape_warning_filter():
    """
    app 初始化过滤器应覆盖 oss2 的无效转义警告。
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        app._filter_third_party_startup_warnings()
        warnings.warn_explicit(
            f'"{chr(92)}&" is an invalid escape sequence.',
            SyntaxWarning,
            filename="oss2/api.py",
            lineno=703,
            module="oss2.api",
        )

    assert caught == []


def test_app_does_not_hide_other_invalid_escape_warnings():
    """其他无效转义仍应暴露，避免过滤器遮蔽新问题。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        app._filter_third_party_startup_warnings()
        warnings.warn_explicit(
            f'"{chr(92)}q" is an invalid escape sequence.',
            SyntaxWarning,
            filename="app/example.py",
            lineno=1,
            module="app.example",
        )

    assert len(caught) == 1


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
