import importlib
import sys
from types import SimpleNamespace

import pytest


def test_ensure_urllib3_header_param_compat_adds_best_available_alias(monkeypatch):
    """urllib3 future 缺少旧别名时，应补齐最接近的新入口。"""
    from app.modules.telegram.compat import ensure_urllib3_header_param_compat

    def multipart_formatter(name, value):
        return f"multipart:{name}={value}"

    def rfc2231_formatter(name, value):
        return f"rfc2231:{name}={value}"

    fake_fields = SimpleNamespace(
        format_multipart_header_param=multipart_formatter,
        format_header_param_rfc2231=rfc2231_formatter,
    )
    fake_urllib3 = SimpleNamespace(fields=fake_fields)
    monkeypatch.setitem(sys.modules, "urllib3", fake_urllib3)

    ensure_urllib3_header_param_compat()

    assert fake_fields.format_header_param is multipart_formatter


def test_ensure_urllib3_header_param_compat_prefers_html5_formatter(monkeypatch):
    """旧版本 urllib3 的 html5 formatter 存在时，应优先保持原别名语义。"""
    from app.modules.telegram.compat import ensure_urllib3_header_param_compat

    def html5_formatter(name, value):
        return f"html5:{name}={value}"

    def multipart_formatter(name, value):
        return f"multipart:{name}={value}"

    fake_fields = SimpleNamespace(
        format_header_param_html5=html5_formatter,
        format_multipart_header_param=multipart_formatter,
    )
    fake_urllib3 = SimpleNamespace(fields=fake_fields)
    monkeypatch.setitem(sys.modules, "urllib3", fake_urllib3)

    ensure_urllib3_header_param_compat()

    assert fake_fields.format_header_param is html5_formatter


def test_ensure_urllib3_header_param_compat_keeps_existing_formatter(monkeypatch):
    """已有 format_header_param 时不应覆盖，避免改变 urllib3 正常行为。"""
    from app.modules.telegram.compat import ensure_urllib3_header_param_compat

    def existing_formatter(name, value):
        return f"existing:{name}={value}"

    def rfc2231_formatter(name, value):
        return f"fallback:{name}={value}"

    fake_fields = SimpleNamespace(
        format_header_param=existing_formatter,
        format_header_param_rfc2231=rfc2231_formatter,
    )
    fake_urllib3 = SimpleNamespace(fields=fake_fields)
    monkeypatch.setitem(sys.modules, "urllib3", fake_urllib3)

    ensure_urllib3_header_param_compat()

    assert fake_fields.format_header_param is existing_formatter


def test_ensure_urllib3_header_param_compat_noops_without_fallback(monkeypatch):
    """没有任何可用 fallback 时保持 no-op，让原始导入错误暴露。"""
    from app.modules.telegram.compat import ensure_urllib3_header_param_compat

    fake_fields = SimpleNamespace()
    fake_urllib3 = SimpleNamespace(fields=fake_fields)
    monkeypatch.setitem(sys.modules, "urllib3", fake_urllib3)

    ensure_urllib3_header_param_compat()

    assert not hasattr(fake_fields, "format_header_param")


def test_telegram_module_imports_when_urllib3_header_alias_missing(monkeypatch):
    """导入 Telegram 模块前旧别名缺失时，应先补齐再导入 telebot。"""
    from urllib3 import fields

    if not any(
        hasattr(fields, name)
        for name in (
            "format_header_param_html5",
            "format_multipart_header_param",
            "format_header_param_rfc2231",
        )
    ):
        pytest.skip("urllib3 has no compatible header formatter fallback")

    monkeypatch.delattr(fields, "format_header_param", raising=False)
    for module_name in list(sys.modules):
        if (
            module_name == "app.modules.telegram.telegram"
            or module_name.startswith("telebot")
        ):
            monkeypatch.delitem(sys.modules, module_name, raising=False)

    telegram_module = importlib.import_module("app.modules.telegram.telegram")

    assert telegram_module.Telegram
    assert hasattr(fields, "format_header_param")
