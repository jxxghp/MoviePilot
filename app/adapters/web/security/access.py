"""把应用安全能力适配为 FastAPI 认证依赖和 Cookie 行为。"""

import datetime
from datetime import timedelta
from typing import Annotated, Any, Callable, Optional

import jwt
from fastapi import HTTPException, Request, Response, Security, status
from fastapi.security import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    HTTPBearer,
    OAuth2PasswordBearer,
)

from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.token import TokenPayload

SuperuserTokenPayloadProvider = Callable[[], TokenPayload]
TokenIdentityValidator = Callable[[TokenPayload], None]
TokenEncoder = Callable[..., str]
TokenDecoder = Callable[[str | None, str], TokenPayload]
_superuser_token_payload_provider: Optional[SuperuserTokenPayloadProvider] = None
_token_identity_validator: Optional[TokenIdentityValidator] = None
_token_encoder: Optional[TokenEncoder] = None
_token_decoder: Optional[TokenDecoder] = None
JWT_ALGORITHM = "HS256"


oauth2_scheme_manual_error = OAuth2PasswordBearer(
    auto_error=False,
    tokenUrl=f"{get_runtime_setting('API_V1_STR')}/login/access-token",
)
resource_token_cookie = APIKeyCookie(
    name=get_runtime_setting('PROJECT_NAME'),
    auto_error=False,
    scheme_name="resource_token_cookie",
)
api_token_query = APIKeyQuery(
    name="token",
    auto_error=False,
    scheme_name="api_token_query",
)
api_key_header = APIKeyHeader(
    name="X-API-KEY",
    auto_error=False,
    scheme_name="api_key_header",
)
api_key_query = APIKeyQuery(
    name="apikey",
    auto_error=False,
    scheme_name="api_key_query",
)
openai_bearer_scheme = HTTPBearer(auto_error=False)
anthropic_api_key_header = APIKeyHeader(
    name="x-api-key",
    auto_error=False,
    scheme_name="anthropic_api_key_header",
)


def set_superuser_token_payload_provider(
    provider: SuperuserTokenPayloadProvider,
) -> None:
    """由启动组合根注入 API 密钥认证使用的超级用户载荷来源。"""
    global _superuser_token_payload_provider
    _superuser_token_payload_provider = provider


def reset_superuser_token_payload_provider() -> None:
    """清除当前 lifespan 的超级用户载荷来源。"""
    global _superuser_token_payload_provider
    _superuser_token_payload_provider = None


def set_token_identity_validator(validator: TokenIdentityValidator) -> None:
    """由启动组合根注入当前令牌身份校验端口。"""
    global _token_identity_validator
    _token_identity_validator = validator


def reset_token_identity_validator() -> None:
    """清除当前 lifespan 的令牌身份校验端口。"""
    global _token_identity_validator
    _token_identity_validator = None


def configure_token_codec(
    encoder: TokenEncoder,
    decoder: TokenDecoder,
) -> None:
    """由组合根注入框架无关的令牌编码与解码能力。"""
    global _token_encoder, _token_decoder
    _token_encoder = encoder
    _token_decoder = decoder


def _encode_token(**claims: Any) -> str:
    """使用已注入编码器创建令牌，未装配时给出明确错误。"""
    if _token_encoder is None:
        raise RuntimeError("Web 认证令牌编码器尚未配置")
    return _token_encoder(**claims)


def _decode_token(token: str | None, purpose: str) -> TokenPayload:
    """使用已注入解码器验证令牌，未装配时给出明确错误。"""
    if _token_decoder is None:
        raise RuntimeError("Web 认证令牌解码器尚未配置")
    return _token_decoder(token, purpose)


def _get_api_token(
    token_query: Annotated[str | None, Security(api_token_query)] = None,
    key_header: Annotated[str | None, Security(api_key_header)] = None,
) -> str | None:
    """优先从请求头、其次从查询参数读取兼容 API Token。"""
    return key_header or token_query


def _get_api_key(
    key_query: Annotated[str | None, Security(api_key_query)] = None,
    key_header: Annotated[str | None, Security(api_key_header)] = None,
) -> str | None:
    """优先从请求头、其次从查询参数读取兼容 API Key。"""
    return key_header or key_query


def _create_superuser_token_payload() -> TokenPayload:
    """使用组合根提供器创建 API 密钥调用的当前超级用户载荷。"""
    if not _superuser_token_payload_provider:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="认证服务尚未初始化",
        )
    try:
        return _superuser_token_payload_provider()
    except PermissionError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error) or "用户权限不足",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


def _validate_token_identity(payload: TokenPayload) -> TokenPayload:
    """校验令牌声明对应的当前账号，并转换应用层失权为 HTTP 401。"""
    if _token_identity_validator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="认证服务尚未初始化",
        )
    try:
        _token_identity_validator(payload)
    except PermissionError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error) or "用户不存在或已禁用",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    return payload


def _validated_superuser_payload() -> TokenPayload:
    """返回经过当前账号和超级管理员权限校验的 API 凭据身份。"""
    # 载荷提供器直接从当前用户记录读取 active/superuser，避免同一请求重复查库。
    return _create_superuser_token_payload()


def validate_api_credential_identity() -> TokenPayload:
    """校验管理员级兼容 API 凭据当前绑定的超级用户身份。"""
    return _validated_superuser_payload()


def set_or_refresh_resource_token_cookie(
    request: Request,
    response: Response,
    payload: TokenPayload,
) -> None:
    """复用匹配的资源令牌，或为当前身份写入新的安全 Cookie。"""
    project_name = get_runtime_setting('PROJECT_NAME')
    resource_token = request.cookies.get(project_name)
    if resource_token:
        try:
            decoded = jwt.decode(
                resource_token,
                get_runtime_setting('RESOURCE_SECRET_KEY'),
                algorithms=[JWT_ALGORITHM],
            )
            exp = decoded.get("exp")
            if exp:
                remaining_time = datetime.datetime.fromtimestamp(
                    exp,
                    tz=datetime.UTC,
                ) - datetime.datetime.now(datetime.UTC)
                if remaining_time < timedelta(
                    seconds=(
                        get_runtime_setting(
                            "RESOURCE_ACCESS_TOKEN_EXPIRE_SECONDS"
                        )
                        / 3
                    )
                ):
                    raise jwt.ExpiredSignatureError
            expected_claims = {
                "sub": str(payload.sub),
                "username": payload.username,
                "super_user": payload.super_user,
                "level": payload.level,
                "purpose": "resource",
            }
            if any(
                decoded.get(claim) != value
                for claim, value in expected_claims.items()
            ):
                raise jwt.InvalidTokenError("资源令牌身份或权限上下文不匹配")
        except jwt.PyJWTError:
            logger.debug("Token error occurred. refreshing token")
        except Exception as error:
            logger.debug(
                f"Unexpected error occurred while decoding token: {error}"
            )
        else:
            return

    resource_token = _encode_token(
        userid=payload.sub,
        username=payload.username or "",
        super_user=payload.super_user,
        expires_delta=timedelta(
            seconds=get_runtime_setting('RESOURCE_ACCESS_TOKEN_EXPIRE_SECONDS')
        ),
        level=payload.level,
        purpose="resource",
    )
    is_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )
    response.set_cookie(
        key=project_name,
        value=resource_token,
        httponly=True,
        secure=is_https,
        samesite="lax",
    )


def _decode_or_http_error(
    token: str | None,
    purpose: str,
) -> TokenPayload:
    """把应用层令牌校验错误转换为带 Bearer 挑战的 HTTP 401。"""
    try:
        return _decode_token(token, purpose)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


def verify_token(
    request: Request,
    response: Response,
    jwt_token: Annotated[
        str | None,
        Security(oauth2_scheme_manual_error),
    ],
    api_key: Annotated[str | None, Security(_get_api_key)],
    api_token: Annotated[str | None, Security(_get_api_token)],
) -> TokenPayload:
    """验证 JWT、API Key 或 API Token，并维护资源 Cookie。"""
    if jwt_token:
        payload = _decode_or_http_error(jwt_token, "authentication")
        _validate_token_identity(payload)
        set_or_refresh_resource_token_cookie(request, response, payload)
        return payload
    if api_key:
        _verify_key(api_key, get_runtime_setting("API_TOKEN"), "apikey")
        return validate_api_credential_identity()
    if api_token:
        _verify_key(api_token, get_runtime_setting("API_TOKEN"), "token")
        return validate_api_credential_identity()
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def verify_resource_token(
    resource_token: Annotated[
        str | None,
        Security(resource_token_cookie),
    ],
) -> TokenPayload:
    """验证 Cookie 中携带的资源访问令牌。"""
    return _validate_token_identity(_decode_or_http_error(resource_token, "resource"))


def _verify_key(key: str | None, expected_key: str, key_type: str) -> str:
    """校验受信第三方集成使用的固定 API 凭据。"""
    if not key or key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{key_type} 校验不通过",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return key


def verify_apitoken(
    token: Annotated[str | None, Security(_get_api_token)],
) -> str:
    """校验请求头或 URL 查询参数中的兼容 API Token。"""
    value = _verify_key(token, get_runtime_setting("API_TOKEN"), "token")
    validate_api_credential_identity()
    return value


def verify_apikey(
    apikey: Annotated[str | None, Security(_get_api_key)],
) -> str:
    """校验请求头或查询参数中的兼容 API Key。"""
    value = _verify_key(apikey, get_runtime_setting("API_TOKEN"), "apikey")
    validate_api_credential_identity()
    return value
