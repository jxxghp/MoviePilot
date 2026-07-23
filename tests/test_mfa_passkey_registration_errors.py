from types import SimpleNamespace
from unittest.mock import patch

import pytest
from webauthn.helpers.exceptions import InvalidRegistrationResponse

from app.api.endpoints import mfa as mfa_endpoint
from app.helper import passkey as passkey_helper
from app.helper.passkey import (
    PassKeyHelper,
    PassKeyRegistrationOriginMismatchError,
    PassKeyRegistrationVerificationError,
    PasskeyChallengeStore,
)


def _registration_request(user_id: int = 1) -> mfa_endpoint.PassKeyRegistrationFinish:
    """构造只用于错误路径的注册完成请求。"""
    transaction_token = PasskeyChallengeStore.issue(
        challenge="challenge",
        purpose="registration",
        user_id=user_id,
    )
    return mfa_endpoint.PassKeyRegistrationFinish(
        credential={"id": "credential-id"},
        transaction_token=transaction_token,
        name="测试通行密钥",
    )


def _current_user() -> SimpleNamespace:
    """构造注册错误路径所需的当前用户契约。"""
    return SimpleNamespace(id=1, name="admin")


def test_passkey_helper_classifies_origin_mismatch():
    """来源不一致应在 WebAuthn 边界转换为稳定的业务异常。"""
    library_error = InvalidRegistrationResponse(
        'Unexpected client data origin "http://localhost:5173", '
        'expected "http://localhost:3000"'
    )

    with patch.object(
        passkey_helper,
        "parse_registration_credential_json",
        return_value=object(),
    ), patch.object(
        passkey_helper,
        "verify_registration_response",
        side_effect=library_error,
    ), pytest.raises(PassKeyRegistrationOriginMismatchError) as exc_info:
        PassKeyHelper.verify_registration_response(
            credential={"id": "credential-id"},
            expected_challenge="Y2hhbGxlbmdl",
        )

    assert exc_info.value.__cause__ is library_error


def test_passkey_helper_classifies_other_verification_failure():
    """其他注册验证错误不应被误判为来源配置问题。"""
    library_error = InvalidRegistrationResponse(
        "Client data challenge was not expected challenge"
    )

    with patch.object(
        passkey_helper,
        "parse_registration_credential_json",
        return_value=object(),
    ), patch.object(
        passkey_helper,
        "verify_registration_response",
        side_effect=library_error,
    ), pytest.raises(PassKeyRegistrationVerificationError) as exc_info:
        PassKeyHelper.verify_registration_response(
            credential={"id": "credential-id"},
            expected_challenge="Y2hhbGxlbmdl",
        )

    assert exc_info.value.__cause__ is library_error


def test_passkey_register_finish_returns_actionable_origin_message():
    """来源配置不一致时应告诉管理员如何修正访问地址。"""
    with patch.object(
        mfa_endpoint.PassKeyHelper,
        "verify_registration_response",
        side_effect=PassKeyRegistrationOriginMismatchError(),
    ):
        response = mfa_endpoint.passkey_register_finish(
            passkey_req=_registration_request(),
            current_user=_current_user(),
        )

    assert not response.success
    assert response.message == "访问域名与系统配置不一致，请使用配置的域名重试"
    assert "APP_DOMAIN" not in response.message
    assert "Unexpected client data origin" not in response.message


def test_passkey_register_finish_hides_other_verification_details():
    """其他 WebAuthn 验证细节只记录在服务端，不返回给客户端。"""
    with patch.object(
        mfa_endpoint.PassKeyHelper,
        "verify_registration_response",
        side_effect=PassKeyRegistrationVerificationError(
            "Client data challenge was not expected challenge"
        ),
    ):
        response = mfa_endpoint.passkey_register_finish(
            passkey_req=_registration_request(),
            current_user=_current_user(),
        )

    assert not response.success
    assert response.message == "通行密钥注册验证失败，请重新发起注册后重试"
    assert "challenge" not in response.message


def test_passkey_register_finish_hides_unexpected_error_details():
    """未知内部异常应返回通用提示，避免泄露实现信息。"""
    with patch.object(
        mfa_endpoint.PassKeyHelper,
        "verify_registration_response",
        side_effect=RuntimeError("database connection details"),
    ):
        response = mfa_endpoint.passkey_register_finish(
            passkey_req=_registration_request(),
            current_user=_current_user(),
        )

    assert not response.success
    assert response.message == "通行密钥注册失败，请稍后重试"
    assert "database" not in response.message
