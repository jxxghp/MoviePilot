from app.runtime.config import settings
from app.adapters.system import rust as rust_accel


class _DummyRustExtension:
    """
    测试用 Rust 扩展替身，用来验证总开关会阻止扩展调用。
    """

    @staticmethod
    def is_available() -> bool:
        """
        模拟 Rust 扩展基础能力可用。
        """
        return True

    @staticmethod
    def parse_filter_rule_fast(_expression: str) -> list:
        """
        如果总开关关闭后仍调用扩展，测试应立即失败。
        """
        raise AssertionError("Rust extension should not be called")


def test_rust_accel_runtime_switch_disables_fast_paths(monkeypatch):
    """
    RUST_ACCEL 关闭时，即便扩展可用也应回退到 Python 路径。
    """
    monkeypatch.setattr(settings, "RUST_ACCEL", False)
    monkeypatch.setattr(rust_accel, "is_required", lambda: False)
    monkeypatch.setattr(rust_accel, "_moviepilot_rust", _DummyRustExtension())

    assert rust_accel.is_available()
    assert not rust_accel.is_enabled()
    assert rust_accel.parse_filter_rule("HDR") is None


def test_free_threaded_runtime_requires_rust_acceleration(monkeypatch):
    """free-threaded 运行时不能通过配置关闭 Rust 快路径。"""
    monkeypatch.setattr(settings, "RUST_ACCEL", False)
    monkeypatch.setattr(rust_accel, "is_required", lambda: True)
    monkeypatch.setattr(rust_accel, "_moviepilot_rust", _DummyRustExtension())

    assert rust_accel.is_config_enabled()
    assert rust_accel.is_enabled()
    assert rust_accel.status()["required"] is True


def test_rust_accel_status_reports_enabled_state(monkeypatch):
    """
    状态接口应同时体现扩展可用性和配置开关后的实际启用状态。
    """
    monkeypatch.setattr(settings, "RUST_ACCEL", True)
    monkeypatch.setattr(rust_accel, "is_required", lambda: False)
    monkeypatch.setattr(rust_accel, "_moviepilot_rust", _DummyRustExtension())

    assert rust_accel.status()["available"] is True
    assert rust_accel.status()["enabled"] is True
