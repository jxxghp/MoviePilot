"""123 云盘客户端代理。

第三方包 ``p123client`` 一律在调用时才导入：插件模块本身必须能在依赖尚未安装的
环境里被导入，否则宿主连「这个插件缺依赖」都报不出来，只会在扫描插件目录时抛一个
与依赖无关的导入错误。
"""

from typing import Any, Callable, Mapping, Optional

# 123 云盘在同一账号并发令牌数超限时返回的业务码与文案
_TOKEN_LIMIT_CODE = 401
_TOKEN_LIMIT_MESSAGE = "tokens number has exceeded the limit"


def check_response(response: Any) -> Any:
    """
    校验接口响应，业务码非成功时抛出异常

    :param response: 接口原始响应
    :return: 原样返回的响应
    :raises ImportError: 依赖 p123client 未安装
    """
    from p123client import check_response as _check_response

    return _check_response(response)


def _is_token_limit(response: Any) -> bool:
    """
    判断响应是否为令牌数超限

    :param response: 接口原始响应
    :return: 响应表示令牌数超限时为 True
    """
    return (
        isinstance(response, Mapping)
        and response.get("code") == _TOKEN_LIMIT_CODE
        and response.get("message") == _TOKEN_LIMIT_MESSAGE
    )


class P123AutoClient:
    """按账号密码持有 123 云盘连接，令牌数超限时自动重建后重试一次。

    连接建立推迟到首次调用：存储后端在没有凭据时也要能被构造出来，用户还没填账号
    的实例照样要出现在存储列表里。

    :param passport: 登录账号，手机号或邮箱
    :param password: 登录密码
    :param factory: 建立底层客户端的可调用对象，缺省时用 ``p123client.P123Client``
    """

    def __init__(
        self,
        passport: str,
        password: str,
        factory: Optional[Callable[[str, str], Any]] = None,
    ) -> None:
        """记录凭据与客户端工厂，暂不建立连接。"""
        self._passport = passport
        self._password = password
        self._factory = factory or _default_client_factory
        self._client: Optional[Any] = None

    def _connect(self) -> Any:
        """
        取得底层客户端，尚未建立时建立一个

        :return: 底层客户端对象
        """
        if self._client is None:
            self._client = self._factory(self._passport, self._password)
        return self._client

    def _reconnect(self) -> Any:
        """
        丢弃当前连接并重新建立一个

        :return: 新建立的底层客户端对象
        """
        self._client = None
        return self._connect()

    def __getattr__(self, name: str) -> Callable[..., Any]:
        """
        代理底层客户端的接口方法，令牌数超限时重建连接并重试一次

        以下划线开头的名字不代理：那是本代理自己的属性，转交给底层客户端会在实例
        属性尚未建立时递归回到本方法。

        :param name: 接口方法名
        :return: 代理后的可调用对象
        :raises AttributeError: 名字以下划线开头
        """
        if name.startswith("_"):
            raise AttributeError(name)

        def invoke(*args, **kwargs) -> Any:
            """调用底层客户端的同名方法，令牌数超限时重试一次。"""
            attribute = getattr(self._connect(), name)
            if not callable(attribute):
                return attribute
            response = attribute(*args, **kwargs)
            if not _is_token_limit(response):
                return response
            retried = getattr(self._reconnect(), name)
            return retried(*args, **kwargs) if callable(retried) else retried

        return invoke


def _default_client_factory(passport: str, password: str) -> Any:
    """
    建立官方客户端

    :param passport: 登录账号
    :param password: 登录密码
    :return: ``p123client.P123Client`` 实例
    :raises ImportError: 依赖 p123client 未安装
    """
    from p123client import P123Client

    return P123Client(passport, password)
