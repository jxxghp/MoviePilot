"""
PassKey WebAuthn 辅助工具类
"""
import base64
import json
import binascii
from typing import Optional, Tuple, List, Dict, Any
from urllib.parse import urlparse

from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json
)
from webauthn.helpers import (
    parse_registration_credential_json,
    parse_authentication_credential_json
)
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor,
    AuthenticatorTransport,
    UserVerificationRequirement,
    AuthenticatorAttachment,
    ResidentKeyRequirement,
    AuthenticatorSelectionCriteria
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier

from app.core.config import settings
from app.log import logger


class PassKeyHelper:
    """
    PassKey WebAuthn 辅助类
    """

    @staticmethod
    def get_rp_id() -> str:
        """
        获取 Relying Party ID
        """
        if settings.APP_DOMAIN:
            app_domain = settings.APP_DOMAIN.strip()
            # 确保存在协议前缀，以便 urlparse 正确解析主机和端口
            if not app_domain.startswith(('http://', 'https://')):
                app_domain = f'https://{app_domain}'
            parsed = urlparse(app_domain)
            host = parsed.hostname
            if host:
                return host
            # 从 APP_DOMAIN 中提取域名
            host = settings.APP_DOMAIN.replace('https://', '').replace('http://', '')
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
        if settings.APP_DOMAIN:
            return settings.APP_DOMAIN.rstrip('/')
        # 如果未配置APP_DOMAIN，使用默认的localhost地址
        return f'http://localhost:{settings.NGINX_PORT}'

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
            exclude_credentials = []
            if existing_credentials:
                for cred in existing_credentials:
                    try:
                        exclude_credentials.append(
                            PublicKeyCredentialDescriptor(
                                id=base64.urlsafe_b64decode(cred['credential_id'] + '=='),
                                transports=[
                                    AuthenticatorTransport(t) for t in cred.get('transports', '').split(',') if t
                                ] if cred.get('transports') else None
                            )
                        )
                    except Exception as e:
                        logger.warning(f"解析凭证失败: {e}")
                        continue

            # 用户验证要求
            uv_requirement = UserVerificationRequirement.REQUIRED if settings.PASSKEY_REQUIRE_UV \
                else UserVerificationRequirement.PREFERRED

            # 生成注册选项
            options = generate_registration_options(
                rp_id=PassKeyHelper.get_rp_id(),
                rp_name=PassKeyHelper.get_rp_name(),
                user_id=user_id_bytes,
                user_name=username,
                user_display_name=display_name or username,
                exclude_credentials=exclude_credentials if exclude_credentials else None,
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
            challenge = base64.urlsafe_b64encode(options.challenge).decode('utf-8').rstrip('=')

            return options_json, challenge

        except Exception as e:
            logger.error(f"生成注册选项失败: {e}")
            raise

    @staticmethod
    def _get_verified_origin(credential: Dict[str, Any], rp_id: str, default_origin: str) -> str:
        """
        在 localhost 环境下获取并验证实际 Origin，否则返回默认值
        """
        if not settings.APP_DOMAIN and rp_id == 'localhost':
            try:
                # 解析 clientDataJSON 获取实际的 origin
                client_data_json = json.loads(
                    base64.urlsafe_b64decode(
                        credential['response']['clientDataJSON'].replace('-', '+').replace('_', '/') + '=='
                    ).decode('utf-8')
                )
                actual_origin = client_data_json.get('origin', '')
                hostname = urlparse(actual_origin).hostname
                
                if hostname in ['localhost', '127.0.0.1']:
                    logger.info(f"本地环境，使用动态 origin: {actual_origin}")
                    return actual_origin
            except Exception as e:
                logger.warning(f"无法提取动态 origin: {e}")
        return default_origin

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
            origin = expected_origin or PassKeyHelper.get_origin()
            rp_id = expected_rp_id or PassKeyHelper.get_rp_id()
            
            # 解码challenge
            challenge_bytes = base64.urlsafe_b64decode(expected_challenge + '==')

            # 构建RegistrationCredential对象
            registration_credential = parse_registration_credential_json(json.dumps(credential))

            # 获取并验证 Origin
            origin = PassKeyHelper._get_verified_origin(credential, rp_id, origin)

            # 验证注册响应
            verification = verify_registration_response(
                credential=registration_credential,
                expected_challenge=challenge_bytes,
                expected_rp_id=rp_id,
                expected_origin=origin,
                require_user_verification=settings.PASSKEY_REQUIRE_UV
            )

            # 提取信息
            credential_id = base64.urlsafe_b64encode(verification.credential_id).decode('utf-8').rstrip('=')
            public_key = base64.urlsafe_b64encode(verification.credential_public_key).decode('utf-8').rstrip('=')
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
            allow_credentials = []
            if existing_credentials:
                for cred in existing_credentials:
                    try:
                        allow_credentials.append(
                            PublicKeyCredentialDescriptor(
                                id=base64.urlsafe_b64decode(cred['credential_id'] + '=='),
                                transports=[
                                    AuthenticatorTransport(t) for t in cred.get('transports', '').split(',') if t
                                ] if cred.get('transports') else None
                            )
                        )
                    except Exception as e:
                        logger.warning(f"解析凭证失败: {e}")
                        continue

            # 用户验证要求
            if not user_verification:
                uv_requirement = UserVerificationRequirement.REQUIRED if settings.PASSKEY_REQUIRE_UV \
                    else UserVerificationRequirement.PREFERRED
            else:
                uv_requirement = UserVerificationRequirement(user_verification)

            # 生成认证选项
            options = generate_authentication_options(
                rp_id=PassKeyHelper.get_rp_id(),
                allow_credentials=allow_credentials if allow_credentials else None,
                user_verification=uv_requirement
            )

            # 转换为JSON
            options_json = options_to_json(options)
            
            # 提取challenge
            challenge = base64.urlsafe_b64encode(options.challenge).decode('utf-8').rstrip('=')

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
            origin = expected_origin or PassKeyHelper.get_origin()
            rp_id = expected_rp_id or PassKeyHelper.get_rp_id()
            
            # 解码
            challenge_bytes = base64.urlsafe_b64decode(expected_challenge + '==')
            public_key_bytes = base64.urlsafe_b64decode(credential_public_key + '==')

            # 构建AuthenticationCredential对象
            authentication_credential = parse_authentication_credential_json(json.dumps(credential))

            # 获取并验证 Origin
            origin = PassKeyHelper._get_verified_origin(credential, rp_id, origin)

            # 验证认证响应
            verification = verify_authentication_response(
                credential=authentication_credential,
                expected_challenge=challenge_bytes,
                expected_rp_id=rp_id,
                expected_origin=origin,
                credential_public_key=public_key_bytes,
                credential_current_sign_count=credential_current_sign_count,
                require_user_verification=settings.PASSKEY_REQUIRE_UV
            )

            return True, verification.new_sign_count

        except Exception as e:
            logger.error(f"验证认证响应失败: {e}")
            return False, credential_current_sign_count
