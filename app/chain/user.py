import secrets
from typing import Optional, Tuple, Union

from app.chain import ChainBase
from app.core.config import settings
from app.core.security import get_password_hash, verify_password
from app.db.models.user import User
from app.db.user_oper import UserOper
from app.log import logger
from app.schemas import AuthCredentials, AuthInterceptCredentials
from app.schemas.types import ChainEventType
from app.utils.otp import OtpUtils
from app.utils.singleton import Singleton

PASSWORD_INVALID_CREDENTIALS_MESSAGE = "用户名或密码或二次校验码不正确"


class UserChain(ChainBase, metaclass=Singleton):
    """
    用户链，处理多种认证协议
    """

    def __init__(self):
        super().__init__()
        self.user_oper = UserOper()

    def user_authenticate(
            self,
            username: Optional[str] = None,
            password: Optional[str] = None,
            mfa_code: Optional[str] = None,
            code: Optional[str] = None,
            grant_type: Optional[str] = "password"
    ) -> Union[Tuple[bool, Optional[str]], Tuple[bool, Optional[User]]]:
        """
        认证用户，根据不同的 grant_type 处理不同的认证流程

        :param username: 用户名，适用于 "password" grant_type
        :param password: 用户密码，适用于 "password" grant_type
        :param mfa_code: 一次性密码，适用于 "password" grant_type
        :param code: 授权码，适用于 "authorization_code" grant_type
        :param grant_type: 认证类型，如 "password", "authorization_code", "client_credentials"
        :return:
            - 对于成功的认证，返回 (True, User)
            - 对于失败的认证，返回 (False, "错误信息")
        """
        credentials = AuthCredentials(
            username=username,
            password=password,
            mfa_code=mfa_code,
            code=code,
            grant_type=grant_type
        )
        logger.debug(f"认证类型：{grant_type}，开始准备对用户 {username} 进行身份校验")
        if credentials.grant_type == "password":
            # Password 认证
            success, user_or_message = self.password_authenticate(credentials=credentials)
            if success:
                # 如果用户启用了二次验证码，则进一步验证
                if not self._verify_mfa(user_or_message, credentials.mfa_code):
                    return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
                logger.info(f"用户 {username} 通过密码认证成功")
                return True, user_or_message
            else:
                # 用户不存在或密码错误，考虑辅助认证
                if settings.AUXILIARY_AUTH_ENABLE:
                    logger.warning("密码认证失败，尝试通过外部服务进行辅助认证 ...")
                    aux_success, aux_user_or_message = self.auxiliary_authenticate(credentials=credentials)
                    if aux_success:
                        # 辅助认证成功后再验证二次验证码
                        if not self._verify_mfa(aux_user_or_message, credentials.mfa_code):
                            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
                        return True, aux_user_or_message
                    else:
                        return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
                else:
                    logger.debug(f"辅助认证未启用，用户 {username} 认证失败")
                    return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
        elif credentials.grant_type == "authorization_code":
            # 处理其他认证类型的分支
            if settings.AUXILIARY_AUTH_ENABLE:
                aux_success, aux_user_or_message = self.auxiliary_authenticate(credentials=credentials)
                if aux_success:
                    return True, aux_user_or_message
                else:
                    return False, "认证失败"
            else:
                return False, "认证失败"
        else:
            logger.debug(f"辅助认证未启用，认证类型 {grant_type} 未实现")
            return False, "不支持的认证类型"

    def password_authenticate(self, credentials: AuthCredentials) -> Tuple[bool, Union[User, str]]:
        """
        密码认证

        :param credentials: 认证凭证，包含用户名、密码以及可选的 MFA 认证码
        :return:
            - 成功时返回 (True, User)，其中 User 是认证通过的用户对象
            - 失败时返回 (False, "错误信息")
        """
        if not credentials or credentials.grant_type != "password":
            logger.info("密码认证失败，认证类型不匹配")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE

        user = self.user_oper.get_by_name(name=credentials.username)
        if not user:
            logger.info(f"密码认证失败，用户 {credentials.username} 不存在")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE

        if not user.is_active:
            logger.info(f"密码认证失败，用户 {credentials.username} 已被禁用")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE

        if not verify_password(credentials.password, str(user.hashed_password)):
            logger.info(f"密码认证失败，用户 {credentials.username} 的密码验证不通过")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE

        return True, user

    def auxiliary_authenticate(self, credentials: AuthCredentials) -> Tuple[bool, Union[User, str]]:
        """
        辅助用户认证

        :param credentials: 认证凭证，包含必要的认证信息
        :return:
            - 成功时返回 (True, User)，其中 User 是认证通过的用户对象
            - 失败时返回 (False, "错误信息")
        """
        if not credentials:
            return False, "认证凭证无效"

        # 检查是否因为用户被禁用
        if credentials.username:
            user = self.user_oper.get_by_name(name=credentials.username)
            if user and not user.is_active:
                logger.info(f"用户 {user.name} 已被禁用，跳过后续身份校验")
                return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE

        logger.debug(f"认证类型：{credentials.grant_type}，尝试通过系统模块进行辅助认证，用户: {credentials.username}")
        result = self.run_module("user_authenticate", credentials=credentials)

        if not result:
            logger.debug(f"通过系统模块辅助认证失败，尝试触发 {ChainEventType.AuthVerification} 事件")
            event = self.eventmanager.send_event(etype=ChainEventType.AuthVerification, data=credentials)
            if not event or not event.event_data:
                logger.error(f"认证类型：{credentials.grant_type}，辅助认证失败，未返回有效数据")
                return False, f"认证类型：{credentials.grant_type}，辅助认证事件失败或无效"

            credentials = event.event_data  # 使用事件返回的认证数据
        else:
            logger.info(f"通过系统模块辅助认证成功，用户: {credentials.username}")
            credentials = result  # 使用模块认证返回的认证数据

        # 处理认证成功的逻辑
        success = self._process_auth_success(username=credentials.username, credentials=credentials)
        if success:
            logger.info(f"用户 {credentials.username} 辅助认证通过")
            return True, self.user_oper.get_by_name(credentials.username)
        else:
            logger.warning(f"用户 {credentials.username} 辅助认证未通过")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE

    @staticmethod
    def _verify_mfa(user: User, mfa_code: Optional[str]) -> bool:
        """
        验证 MFA（二次验证码）

        :param user: 用户对象
        :param mfa_code: 二次验证码
        :return: 如果验证成功返回 True，否则返回 False
        """
        if not user.is_otp:
            return True
        if not mfa_code:
            logger.info(f"用户 {user.name} 缺少 MFA 认证码")
            return False
        if not OtpUtils.check(str(user.otp_secret), mfa_code):
            logger.info(f"用户 {user.name} 的 MFA 认证失败")
            return False
        return True

    def _process_auth_success(self, username: str, credentials: AuthCredentials) -> bool:
        """
        处理辅助认证成功的逻辑，返回用户对象或创建新用户

        :param username: 用户名
        :param credentials: 认证凭证，包含 token、channel、service 等信息
        :return:
            - 如果认证成功并且用户存在或已创建，返回 User 对象
            - 如果认证被拦截或失败，返回 None
        """
        if not username:
            logger.info(f"未能获取到对应的用户信息，{credentials.grant_type} 认证不通过")
            return False

        token, channel, service = credentials.token, credentials.channel, credentials.service
        if not all([token, channel, service]):
            logger.info(f"用户 {username} 未通过 {credentials.grant_type} 认证，必要信息不足")
            return False

        # 触发认证通过的拦截事件
        intercept_event = self.eventmanager.send_event(
            etype=ChainEventType.AuthIntercept,
            data=AuthInterceptCredentials(username=username, channel=channel, service=service,
                                          token=token, status="completed")
        )
        if intercept_event and intercept_event.event_data:
            intercept_data: AuthInterceptCredentials = intercept_event.event_data
            if intercept_data.cancel:
                logger.warning(
                    f"认证被拦截，用户：{username}，渠道：{channel}，服务：{service}，拦截源：{intercept_data.source}")
                return False

        # 检查用户是否存在，如果不存在且当前为密码认证时则创建新用户
        user = self.user_oper.get_by_name(name=username)
        if user:
            # 如果用户存在，但是已经被禁用，则直接响应
            if not user.is_active:
                logger.info(f"辅助认证失败，用户 {username} 已被禁用")
                return False
            anonymized_token = f"{token[:len(token) // 2]}********"
            logger.info(
                f"认证类型：{credentials.grant_type}，用户：{username}，渠道：{channel}，"
                f"服务：{service} 认证成功，token：{anonymized_token}")
            return True
        else:
            if credentials.grant_type == "password":
                self.user_oper.add(name=username, is_active=True, is_superuser=False,
                                   hashed_password=get_password_hash(secrets.token_urlsafe(16)))
                logger.info(f"用户 {username} 不存在，已通过 {credentials.grant_type} 认证并已创建普通用户")
                return True
            else:
                logger.warning(
                    f"认证类型：{credentials.grant_type}，用户：{username}，渠道：{channel}，"
                    f"服务：{service} 认证不通过，未能在本地找到对应的用户信息")
                return False
