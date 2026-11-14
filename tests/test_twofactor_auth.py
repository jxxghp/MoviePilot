"""
自动生成的 TOTP 必须保留前导零，手工验证码仍按原值使用。
"""

import pyotp
import pytest

from app.application.security import twofactor

# 公开测试密钥，不连接真实账户或认证服务。
TEST_SECRET = "JBSWY3DPEHPK3PXP"


@pytest.mark.parametrize("timestamp", [0, 30, 4020, 4830, 7680])
def test_generated_totp_preserves_all_six_digits(monkeypatch, timestamp):
    """
    与独立 TOTP 实现对照普通码及含多个前导零的码。
    """
    monkeypatch.setattr(twofactor.time, "time", lambda: timestamp)

    code = twofactor.TwoFactorAuth(TEST_SECRET).get_code()

    assert code == pyotp.TOTP(TEST_SECRET).at(timestamp)
    assert len(code) == 6
    assert code.isdecimal()


@pytest.mark.parametrize("code", ["123456", "000001", "8210"])
def test_explicit_code_is_returned_unchanged(code):
    """
    补零只作用于自动生成的码，不改写用户直接提供的验证码。
    """
    assert twofactor.TwoFactorAuth(code).get_code() == code


@pytest.mark.parametrize("code", [None, ""])
def test_empty_code_or_secret_preserves_empty_result(code):
    """
    缺少手工验证码和共享密钥时继续返回空字符串。
    """
    assert twofactor.TwoFactorAuth(code).get_code() == ""


def test_invalid_secret_preserves_error_log_and_empty_result(monkeypatch):
    """
    非法密钥仍记录错误并返回空值，不向调用方抛出认证异常。
    """
    messages = []
    monkeypatch.setattr(twofactor.logger, "error", messages.append)

    assert twofactor.TwoFactorAuth("not-a-valid-secret!").get_code() == ""
    assert any("计算动态验证码失败" in message for message in messages)
