from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.api.endpoints import mfa as mfa_endpoint
from app.application.security.passkey import (
    PASSKEY_CHALLENGE_TTL_SECONDS,
    PasskeyChallengeStore,
    configure_passkey_challenge_cache,
)
from app.runtime.cache import TTLCache


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
    configure_passkey_challenge_cache(TTLCache(
        region="passkey_challenge",
        maxsize=4096,
        ttl=PASSKEY_CHALLENGE_TTL_SECONDS,
    ))


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


def test_authentication_finish_fails_closed_when_challenge_backend_fails(
    monkeypatch,
):
    """challenge 后端故障不得继续查询凭证或签发 Token。"""
    class FailingCache:
        """模拟 challenge 原子领取期间缓存不可用。"""

        def consume(self, _key):
            """报告后端不可用。"""
            raise RuntimeError("cache unavailable")

    monkeypatch.setattr(PasskeyChallengeStore, "_cache", FailingCache())
    service = SimpleNamespace(get_by_credential_id=Mock())
    auth_service = SimpleNamespace(build_token_response=Mock())

    with patch.object(
        mfa_endpoint,
        "get_configured_auth_service",
        return_value=auth_service,
    ), patch.object(
        mfa_endpoint,
        "set_or_refresh_resource_token_cookie",
    ) as set_cookie, pytest.raises(HTTPException) as exc_info:
        mfa_endpoint.passkey_authenticate_finish(
            request=_request(),
            response=Response(),
            passkey_req=mfa_endpoint.PassKeyAuthenticationFinish(
                credential={"id": "credential-id"},
                transaction_token="transaction-token",
            ),
            service=service,
        )

    assert exc_info.value.status_code == 401
    service.get_by_credential_id.assert_not_called()
    auth_service.build_token_response.assert_not_called()
    set_cookie.assert_not_called()


@pytest.mark.parametrize("cas_failure", [False, RuntimeError("write failed")])
def test_authentication_finish_does_not_issue_token_when_sign_count_write_fails(
    cas_failure,
):
    """签名计数 CAS 冲突或事务失败时，认证不得越过持久化门禁。"""
    token = PasskeyChallengeStore.issue(
        challenge="server-challenge",
        purpose="authentication",
        user_id=1,
    )
    passkey_req = mfa_endpoint.PassKeyAuthenticationFinish(
        credential={"id": "credential-id"},
        transaction_token=token,
    )
    passkey = SimpleNamespace(
        id=10,
        user_id=1,
        public_key="public-key",
        sign_count=5,
    )
    user = SimpleNamespace(
        id=1,
        name="user",
        is_active=True,
        is_superuser=False,
    )
    compare_and_update = Mock()
    if isinstance(cas_failure, Exception):
        compare_and_update.side_effect = cas_failure
    else:
        compare_and_update.return_value = cas_failure
    service = SimpleNamespace(
        get_by_credential_id=Mock(return_value=passkey),
        compare_and_update_sign_count=compare_and_update,
    )
    auth_service = SimpleNamespace(build_token_response=Mock())

    with patch.object(
        mfa_endpoint,
        "_extract_and_standardize_credential_id",
        return_value="credential-id",
    ), patch.object(
        mfa_endpoint,
        "get_configured_user_id_lookup",
        return_value=Mock(return_value=user),
    ), patch.object(
        mfa_endpoint.PassKeyHelper,
        "verify_authentication_response",
        return_value=(True, 6),
    ), patch.object(
        mfa_endpoint,
        "get_configured_auth_service",
        return_value=auth_service,
    ), patch.object(
        mfa_endpoint,
        "set_or_refresh_resource_token_cookie",
    ) as set_cookie:
        with pytest.raises(HTTPException) as exc_info:
            mfa_endpoint.passkey_authenticate_finish(
                request=_request(),
                response=Response(),
                passkey_req=passkey_req,
                service=service,
            )

    assert exc_info.value.status_code == 401
    compare_and_update.assert_called_once_with(
        passkey_id=10,
        expected_sign_count=5,
        sign_count=6,
    )
    auth_service.build_token_response.assert_not_called()
    set_cookie.assert_not_called()
