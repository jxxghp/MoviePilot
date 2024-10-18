from app.chain import ChainBase
from app.log import logger
from app.schemas.event import AuthPassedInterceptData, AuthVerificationData
from app.schemas.types import ChainEventType


class UserChain(ChainBase):
    """
    用户链
    """

    def user_authenticate(self, name: str, password: str) -> bool:
        """
        辅助完成用户认证。

        :param name: 用户名
        :param password: 密码
        :return: 认证成功时返回 True，否则返回 False
        """
        logger.debug(f"开始对用户 {name} 通过系统预置渠道进行辅助认证")
        auth_data = AuthVerificationData(name=name, password=password)
        # 尝试通过默认的认证模块认证
        try:
            result = self.run_module("user_authenticate", auth_data=auth_data)
            if result:
                return self._process_auth_success(name, result)
        except Exception as e:
            logger.error(f"认证模块运行出错：{e}")
            return False

        # 如果预置的认证未通过，则触发 AuthVerification 事件
        logger.debug(f"用户 {name} 未通过系统预置渠道认证，触发认证事件")
        event = self.eventmanager.send_event(
            etype=ChainEventType.AuthVerification,
            data=auth_data
        )
        if not event:
            return False
        if event and event.event_data:
            try:
                return self._process_auth_success(name, event.event_data)
            except Exception as e:
                logger.error(f"AuthVerificationData 数据验证失败：{e}")
                return False

        # 认证失败
        logger.warning(f"用户 {name} 辅助认证失败")
        return False

    def _process_auth_success(self, name: str, data: AuthVerificationData) -> bool:
        """
        处理认证成功后的逻辑，记录日志并处理拦截事件。

        :param name: 用户名
        :param data: 认证返回的数据，包含 token、channel 和 service
        :return: 成功返回 True，若被拦截返回 False
        """
        token, channel, service = data.token, data.channel, data.service
        if token and channel and service:
            # 匿名化 token
            anonymized_token = f"{token[:len(token) // 2]}****"
            logger.info(f"用户 {name} 通过渠道 {channel}，服务: {service} 认证成功，token: {anonymized_token}")

            # 触发认证通过的拦截事件
            intercept_event = self.eventmanager.send_event(
                etype=ChainEventType.AuthPassedIntercept,
                data=AuthPassedInterceptData(name=name, channel=channel, service=service, token=token)
            )

            if intercept_event and intercept_event.event_data:
                intercept_data: AuthPassedInterceptData = intercept_event.event_data
                if intercept_data.cancel:
                    logger.info(
                        f"认证被拦截，用户: {name}，渠道: {channel}，服务: {service}，拦截源: {intercept_data.source}")
                    return False

            return True

        logger.warning(f"用户 {name} 未通过辅助认证")
        return False
