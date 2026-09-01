"""
PassKey WebAuthn 辅助工具类
"""
import base64
import binascii
import json
import secrets
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Protocol, Tuple
from urllib.parse import urlparse

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import parse_authentication_credential_json, parse_registration_credential_json
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.application.configuration import get_api_runtime_config_snapshot
from app.runtime.log import logger

PASSKEY_CHALLENGE_TTL_SECONDS = 5 * 60
PasskeyChallengePurpose = Literal["authentication", "registration"]


@dataclass(frozen=True)
class PasskeyChallenge:
    """服务端保存的一次性 Passkey challenge 及其认证边界。"""

    challenge: str
    purpose: PasskeyChallengePurpose
    user_id: Optional[int]


class PasskeyChallengeCache(Protocol):
    """PassKey 一次性 challenge 使用的严格原子缓存端口。"""

    def store(self, key: str, value: Any) -> None:
        """持久化 challenge，失败时抛出后端异常。"""

    def consume(self, key: str) -> Any:
        """原子领取 challenge，不存在时返回 None。"""


class PasskeyChallengeStore:
    """使用当前缓存后端签发并原子消费短时 Passkey challenge。"""

    _cache: Optional[PasskeyChallengeCache] = None

    @classmethod
    def _get_cache(cls) -> PasskeyChallengeCache:
        """返回已装配缓存，缺失时拒绝签发认证状态。"""
        if cls._cache is None:
            raise RuntimeError("PassKey challenge 缓存尚未配置")
        return cls._cache

    @classmethod
    def issue(
        cls,
        *,
        challenge: str,
        purpose: PasskeyChallengePurpose,
        user_id: Optional[int],
    ) -> str:
        """保存 challenge 并返回不携带认证事实的随机事务 token。"""
        transaction_token = secrets.token_urlsafe(32)
        cls._get_cache().store(
            transaction_token,
            PasskeyChallenge(
                challenge=challenge,
                purpose=purpose,
                user_id=user_id,
            ),
        )
        return transaction_token

    @classmethod
    def consume(
        cls,
        *,
        transaction_token: str,
        purpose: PasskeyChallengePurpose,
    ) -> Optional[PasskeyChallenge]:
        """原子领取 challenge；任何完成尝试都会使事务失效。"""
        if not transaction_token:
            return None

        challenge = cls._get_cache().consume(transaction_token)

        if not isinstance(challenge, PasskeyChallenge):
            return None
        if challenge.purpose != purpose:
            return None
        return challenge


def configure_passkey_challenge_cache(cache: PasskeyChallengeCache) -> None:
    """由启动组合根注入 PassKey challenge 的原子缓存。"""
    PasskeyChallengeStore._cache = cache


class PassKeyRegistrationVerificationError(Exception):
    """Passkey 注册响应未通过 WebAuthn 安全校验。"""


class PassKeyRegistrationOriginMismatchError(PassKeyRegistrationVerificationError):
    """浏览器来源与系统配置的 Passkey 注册来源不一致。"""


class PassKeyHelper:
    """
    PassKey WebAuthn 辅助类
    """

    @staticmethod
    def get_rp_id() -> str:
        """
        获取 Relying Party ID
        """
        config = get_api_runtime_config_snapshot()
        if config.app_domain:
            app_domain = config.app_domain.strip()
            # 确保存在协议前缀，以便 urlparse 正确解析主机和端口
            if not app_domain.startswith(('http://', 'https://')):
                app_domain = f'https://{app_domain}'
            parsed = urlparse(app_domain)
            host = parsed.hostname
            if host:
                return host
            # 从 APP_DOMAIN 中提取域名
            host = config.app_domain.replace('https://', '').replace('http://', '')
            # 移除端口号
            if ':' in host:
                host = host.split(':')[0]
            return host
        # 只有在未配置 APP_DOMAIN 时，才默认为 localhost
        return 'localhost'

    @staticmethod
    def get_rp_name() -> str:
        """
        获取 Relying Party 名称
        """
        return "MoviePilot"

    @staticmethod
    def get_origin() -> str:
        """
        获取源地址
        """
        config = get_api_runtime_config_snapshot()
        if config.app_domain:
            return config.app_domain.rstrip('/')
        # 如果未配置APP_DOMAIN，使用默认的localhost地址
        return f'http://localhost:{config.nginx_port}'

    @staticmethod
    def standardize_credential_id(credential_id: str) -> str:
        """
        标准化凭证ID（Base64 URL Safe）
        """
        try:
            # Base64解码并重新编码以标准化格式
            decoded = base64.urlsafe_b64decode(credential_id + '==')
            return base64.urlsafe_b64encode(decoded).decode('utf-8').rstrip('=')
        except (binascii.Error, TypeError, ValueError) as e:
            logger.error(f"标准化凭证ID失败: {e}")
            return credential_id

    @staticmethod
    def _base64_encode_urlsafe(data: bytes) -> str:
        """
        Base64 URL Safe 编码（不带填充）

        :param data: 要编码的字节数据
        :return: Base64 URL Safe 编码的字符串
        """
        return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')

    @staticmethod
    def _base64_decode_urlsafe(data: str) -> bytes:
        """
        Base64 URL Safe 解码（自动添加填充）

        :param data: Base64 URL Safe 编码的字符串
        :return: 解码后的字节数据
        """
        return base64.urlsafe_b64decode(data + '==')

    @staticmethod
    def _parse_credential_list(credentials: List[Dict[str, Any]]) -> List[PublicKeyCredentialDescriptor]:
        """
        解析凭证列表为 PublicKeyCredentialDescriptor 列表

        :param credentials: 凭证字典列表
        :return: PublicKeyCredentialDescriptor 列表
        """
        result = []
        for cred in credentials:
            try:
                result.append(
                    PublicKeyCredentialDescriptor(
                        id=PassKeyHelper._base64_decode_urlsafe(cred['credential_id']),
                        transports=[
                            AuthenticatorTransport(t) for t in cred.get('transports', '').split(',') if t
                        ] if cred.get('transports') else None
                    )
                )
            except Exception as e:
                logger.warning(f"解析凭证失败: {e}")
                continue
        return result

    @staticmethod
    def _get_user_verification_requirement(user_verification: Optional[str] = None) -> UserVerificationRequirement:
        """
        获取用户验证要求

        :param user_verification: 指定的用户验证要求，如果不指定则从配置中读取
        :return: UserVerificationRequirement
        """
        if user_verification:
            return UserVerificationRequirement(user_verification)
        return UserVerificationRequirement.REQUIRED if get_api_runtime_config_snapshot().passkey_require_uv \
            else UserVerificationRequirement.PREFERRED

    @staticmethod
    def _get_verification_params(
        expected_origin: Optional[str] = None,
        expected_rp_id: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        获取验证参数（origin 和 rp_id）

        :param expected_origin: 期望的源地址
        :param expected_rp_id: 期望的RP ID
        :return: (origin, rp_id)
        """
        origin = expected_origin or PassKeyHelper.get_origin()
        rp_id = expected_rp_id or PassKeyHelper.get_rp_id()
        return origin, rp_id

    @staticmethod
    def generate_registration_options(
        user_id: int,
        username: str,
        display_name: Optional[str] = None,
        existing_credentials: Optional[List[Dict[str, Any]]] = None
    ) -> Tuple[str, str]:
        """
        生成注册选项
        
        :param user_id: 用户ID
        :param username: 用户名
        :param display_name: 显示名称
        :param existing_credentials: 已存在的凭证列表
        :return: (options_json, challenge)
        """
        try:
            # 用户信息
            user_id_bytes = str(user_id).encode('utf-8')

            # 排除已有的凭证
            exclude_credentials = PassKeyHelper._parse_credential_list(existing_credentials) \
                if existing_credentials else None

            # 用户验证要求
            uv_requirement = PassKeyHelper._get_user_verification_requirement()

            # 生成注册选项
            options = generate_registration_options(
                rp_id=PassKeyHelper.get_rp_id(),
                rp_name=PassKeyHelper.get_rp_name(),
                user_id=user_id_bytes,
                user_name=username,
                user_display_name=display_name or username,
                exclude_credentials=exclude_credentials,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    authenticator_attachment=None,
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=uv_requirement,
                ),
                supported_pub_key_algs=[
                    COSEAlgorithmIdentifier.ECDSA_SHA_256,
                    COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
                ]
            )

            # 转换为JSON
            options_json = options_to_json(options)

            # 提取challenge（用于后续验证）
            challenge = PassKeyHelper._base64_encode_urlsafe(options.challenge)

            return options_json, challenge

        except Exception as e:
            logger.error(f"生成注册选项失败: {e}")
            raise

    @staticmethod
    def verify_registration_response(
        credential: Dict[str, Any],
        expected_challenge: str,
        expected_origin: Optional[str] = None,
        expected_rp_id: Optional[str] = None
    ) -> Tuple[str, str, int, Optional[str]]:
        """
        验证注册响应
        
        :param credential: 客户端返回的凭证
        :param expected_challenge: 期望的challenge
        :param expected_origin: 期望的源地址
        :param expected_rp_id: 期望的RP ID
        :return: (credential_id, public_key, sign_count, aaguid)
        """
        try:
            # 准备验证参数
            origin, rp_id = PassKeyHelper._get_verification_params(expected_origin, expected_rp_id)
            # 解码challenge
            challenge_bytes = PassKeyHelper._base64_decode_urlsafe(expected_challenge)

            # 构建RegistrationCredential对象
            registration_credential = parse_registration_credential_json(json.dumps(credential))

            # 验证注册响应
            verification = verify_registration_response(
                credential=registration_credential,
                expected_challenge=challenge_bytes,
                expected_rp_id=rp_id,
                expected_origin=origin,
                require_user_verification=get_api_runtime_config_snapshot().passkey_require_uv
            )

            # 提取信息
            credential_id = PassKeyHelper._base64_encode_urlsafe(verification.credential_id)
            public_key = PassKeyHelper._base64_encode_urlsafe(verification.credential_public_key)
            sign_count = verification.sign_count
            # aaguid 可能已经是字符串格式，也可能是bytes
            if verification.aaguid:
                if isinstance(verification.aaguid, bytes):
                    aaguid = verification.aaguid.hex()
                else:
                    aaguid = str(verification.aaguid)
            else:
                aaguid = None

            return credential_id, public_key, sign_count, aaguid

        except InvalidRegistrationResponse as e:
            logger.error(f"验证注册响应失败: {e}")
            if str(e).startswith("Unexpected client data origin "):
                raise PassKeyRegistrationOriginMismatchError() from e
            raise PassKeyRegistrationVerificationError() from e
        except Exception as e:
            logger.error(f"验证注册响应失败: {e}")
            raise

    @staticmethod
    def generate_authentication_options(
        existing_credentials: Optional[List[Dict[str, Any]]] = None,
        user_verification: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        生成认证选项
        
        :param existing_credentials: 已存在的凭证列表（用于限制可用凭证）
        :param user_verification: 用户验证要求，如果不指定则从配置中读取
        :return: (options_json, challenge)
        """
        try:
            # 允许的凭证
            allow_credentials = PassKeyHelper._parse_credential_list(existing_credentials) \
                if existing_credentials else None

            # 用户验证要求
            uv_requirement = PassKeyHelper._get_user_verification_requirement(user_verification)

            # 生成认证选项
            options = generate_authentication_options(
                rp_id=PassKeyHelper.get_rp_id(),
                allow_credentials=allow_credentials,
                user_verification=uv_requirement
            )

            # 转换为JSON
            options_json = options_to_json(options)

            # 提取challenge
            challenge = PassKeyHelper._base64_encode_urlsafe(options.challenge)

            return options_json, challenge

        except Exception as e:
            logger.error(f"生成认证选项失败: {e}")
            raise

    @staticmethod
    def verify_authentication_response(
        credential: Dict[str, Any],
        expected_challenge: str,
        credential_public_key: str,
        credential_current_sign_count: int,
        expected_origin: Optional[str] = None,
        expected_rp_id: Optional[str] = None
    ) -> Tuple[bool, int]:
        """
        验证认证响应
        
        :param credential: 客户端返回的凭证
        :param expected_challenge: 期望的challenge
        :param credential_public_key: 凭证公钥
        :param credential_current_sign_count: 当前签名计数
        :param expected_origin: 期望的源地址
        :param expected_rp_id: 期望的RP ID
        :return: (验证成功, 新的签名计数)
        """
        try:
            # 准备验证参数
            origin, rp_id = PassKeyHelper._get_verification_params(expected_origin, expected_rp_id)
            # 解码
            challenge_bytes = PassKeyHelper._base64_decode_urlsafe(expected_challenge)
            public_key_bytes = PassKeyHelper._base64_decode_urlsafe(credential_public_key)

            # 构建AuthenticationCredential对象
            authentication_credential = parse_authentication_credential_json(json.dumps(credential))

            # 验证认证响应
            verification = verify_authentication_response(
                credential=authentication_credential,
                expected_challenge=challenge_bytes,
                expected_rp_id=rp_id,
                expected_origin=origin,
                credential_public_key=public_key_bytes,
                credential_current_sign_count=credential_current_sign_count,
                require_user_verification=get_api_runtime_config_snapshot().passkey_require_uv
            )

            return True, verification.new_sign_count

        except Exception as e:
            logger.error(f"验证认证响应失败: {e}")
            return False, credential_current_sign_count


class PasskeyRepository(Protocol):
    """PassKey 用例需要的最小同步数据端口。"""

    def list(self) -> list[Any]:
        """列出全部启用凭证。"""

    def list_by_user_id(
        self,
        user_id: int,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> List[Any]:
        """按可选窗口列出指定用户凭证。"""

    def count_by_user_id(self, user_id: int) -> int:
        """返回指定用户启用凭证总数。"""

    def get_by_credential_id(self, credential_id: str) -> Optional[Any]:
        """按凭证 ID 查找凭证。"""

    def create(self, payload: dict[str, Any]) -> Any:
        """创建凭证。"""

    def compare_and_update_sign_count(
        self,
        passkey_id: int,
        expected_sign_count: int,
        sign_count: int,
    ) -> bool:
        """仅在签名计数未被并发修改时记录本次认证。"""

    def delete_by_id(self, passkey_id: int, user_id: int) -> bool:
        """删除用户凭证。"""


class PasskeyService:
    """编排 PassKey 凭证生命周期。"""

    def __init__(self, repository: PasskeyRepository) -> None:
        """注入 PassKey 数据端口。"""
        self._repository = repository

    def list(self) -> list[Any]:
        """列出全部启用凭证。"""
        return self._repository.list()

    def list_by_user_id(
        self,
        user_id: int,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> List[Any]:
        """按可选数据库窗口列出指定用户凭证。"""
        if page is None and count is None:
            return self._repository.list_by_user_id(user_id)
        return self._repository.list_by_user_id(user_id, page=page, count=count)

    def count_by_user_id(self, user_id: int) -> int:
        """返回指定用户启用凭证精确总数。"""
        return self._repository.count_by_user_id(user_id)

    def get_by_credential_id(self, credential_id: str) -> Optional[Any]:
        """按凭证 ID 查找凭证。"""
        return self._repository.get_by_credential_id(credential_id)

    def create(self, payload: dict[str, Any]) -> Any:
        """创建凭证。"""
        return self._repository.create(payload)

    def compare_and_update_sign_count(
        self,
        passkey_id: int,
        expected_sign_count: int,
        sign_count: int,
    ) -> bool:
        """以验证时观察到的旧计数提交本次认证。"""
        return self._repository.compare_and_update_sign_count(
            passkey_id=passkey_id,
            expected_sign_count=expected_sign_count,
            sign_count=sign_count,
        )

    def delete_by_id(self, passkey_id: int, user_id: int) -> bool:
        """删除用户凭证。"""
        return self._repository.delete_by_id(passkey_id, user_id)


_configured_passkey_service: Optional[PasskeyService] = None


def configure_passkey_service(service: PasskeyService) -> None:
    """由启动组合根登记 PassKey 应用服务。"""
    global _configured_passkey_service
    _configured_passkey_service = service


def reset_passkey_challenge_cache() -> None:
    """清除当前 lifespan 的 PassKey challenge 缓存。"""
    PasskeyChallengeStore._cache = None


def reset_passkey_service() -> None:
    """清除当前 lifespan 的 PassKey 应用服务。"""
    global _configured_passkey_service
    _configured_passkey_service = None


def get_configured_passkey_service() -> PasskeyService:
    """返回启动阶段登记的 PassKey 应用服务。"""
    if _configured_passkey_service is None:
        raise RuntimeError("PassKey 服务尚未配置")
    return _configured_passkey_service
