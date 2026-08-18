from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.api.endpoints import mfa as mfa_endpoint
from app.application.security.passkey import PasskeyChallengeStore


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/mfa/passkey/authenticate/finish",
            "headers": [(b"host", b"testserver")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        }
    )


def setup_function():
    PasskeyChallengeStore._cache.clear()


def test_registration_transaction_is_bound_to_current_user():
    token = PasskeyChallengeStore.issue(
        challenge="server-challenge",
        purpose="registration",
        user_id=1,
    )
    request = mfa_endpoint.PassKeyRegistrationFinish(
        credential={"id": "credential-id"},
        transaction_token=token,
        name="test",
    )

    with patch.object(
        mfa_endpoint.PassKeyHelper,
        "verify_registration_response",
    ) as verify:
        result = mfa_endpoint.passkey_register_finish(
            passkey_req=request,
            current_user=SimpleNamespace(id=2, name="other"),
        )

    assert not result.success
    assert result.message == "注册请求已失效，请重新发起注册"
    verify.assert_not_called()


def test_registration_uses_server_challenge():
    token = PasskeyChallengeStore.issue(
        challenge="server-challenge",
        purpose="registration",
        user_id=1,
    )
    request = mfa_endpoint.PassKeyRegistrationFinish(
        credential={"id": "credential-id", "challenge": "client-challenge"},
        transaction_token=token,
        name="test",
    )
    passkey = Mock()
    service = SimpleNamespace(create=Mock(return_value=passkey))

    with patch.object(
        mfa_endpoint.PassKeyHelper,
        "verify_registration_response",
        return_value=("credential-id", b"public-key", 0, "aaguid"),
    ) as verify:
        result = mfa_endpoint.passkey_register_finish(
            passkey_req=request,
            current_user=SimpleNamespace(id=1, name="user"),
            service=service,
        )

    assert result.success
    verify.assert_called_once_with(
        credential=request.credential,
        expected_challenge="server-challenge",
    )
    service.create.assert_called_once()


def test_authentication_transaction_rejects_other_user_credential():
    token = PasskeyChallengeStore.issue(
        challenge="server-challenge",
        purpose="authentication",
        user_id=1,
    )
    request = mfa_endpoint.PassKeyAuthenticationFinish(
        credential={"id": "credential-id"},
        transaction_token=token,
    )
    passkey = SimpleNamespace(user_id=2)
    user = SimpleNamespace(id=2, is_active=True)

    service = SimpleNamespace(get_by_credential_id=Mock(return_value=passkey))
    lookup = Mock(return_value=user)
    with patch.object(
        mfa_endpoint,
        "_extract_and_standardize_credential_id",
        return_value="credential-id",
    ), patch.object(
        mfa_endpoint,
        "get_configured_user_name_lookup",
        return_value=lookup,
    ), patch.object(
        mfa_endpoint,
        "_verify_passkey_and_update",
    ) as verify:
        with pytest.raises(HTTPException) as exc_info:
            mfa_endpoint.passkey_authenticate_finish(
                request=_request(),
                response=Response(),
                passkey_req=request,
                service=service,
            )

    assert exc_info.value.status_code == 401
    verify.assert_not_called()


def test_authentication_finish_token_cannot_be_replayed():
    token = PasskeyChallengeStore.issue(
        challenge="server-challenge",
        purpose="authentication",
        user_id=1,
    )
    request = mfa_endpoint.PassKeyAuthenticationFinish(
        credential={"id": "credential-id"},
        transaction_token=token,
    )
    passkey = SimpleNamespace(user_id=1)
    user = SimpleNamespace(
        id=1,
        name="user",
        is_active=True,
        is_superuser=False,
        avatar="",
        permissions={},
    )

    service = SimpleNamespace(get_by_credential_id=Mock(return_value=passkey))
    lookup = Mock(return_value=user)
    token_response = SimpleNamespace(access_token="access-token", level=1)
    with patch.object(
        mfa_endpoint,
        "_extract_and_standardize_credential_id",
        return_value="credential-id",
    ), patch.object(
        mfa_endpoint,
        "get_configured_user_id_lookup",
        return_value=lookup,
    ), patch.object(
        mfa_endpoint,
        "_verify_passkey_and_update",
        return_value=(True, 0),
    ), patch.object(
        mfa_endpoint,
        "get_configured_auth_service",
        return_value=SimpleNamespace(build_token_response=Mock(return_value=token_response)),
    ), patch.object(
        mfa_endpoint,
        "set_or_refresh_resource_token_cookie",
    ):
        result = mfa_endpoint.passkey_authenticate_finish(
            request=_request(),
            response=Response(),
            passkey_req=request,
            service=service,
        )
        with pytest.raises(HTTPException) as replay_error:
            mfa_endpoint.passkey_authenticate_finish(
                request=_request(),
                response=Response(),
                passkey_req=request,
                service=service,
            )

    assert result.access_token == "access-token"
    assert replay_error.value.status_code == 401
    assert replay_error.value.detail == "认证请求已失效"
