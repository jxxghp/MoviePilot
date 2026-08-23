"""把旧订阅 Oper 写入调用转交给新的应用服务。"""

from typing import Any, Optional

from app.application.subscription.write import add_subscribe, async_add_subscribe
from app.db.oper.subscribe import SubscribeOper as CanonicalSubscribeOper
from app.domain.context import MediaInfo, MusicInfo


class SubscribeOper(CanonicalSubscribeOper):
    """保留旧 ``mediainfo`` 写入签名，同时继承新的查询接口。"""

    def add(
            self,
            mediainfo: Optional[MediaInfo | MusicInfo] = None,
            **kwargs: Any,
    ):
        """
        兼容旧订阅写入；应用服务回调的新字典签名直接交给 canonical Oper。

        :param mediainfo: 旧调用传入的媒体识别结果
        :param kwargs: 旧订阅配置，或新 Oper 的 identity/payload/username
        :return: 订阅 ID 与结果说明
        """
        if mediainfo is None and "identity" in kwargs and "payload" in kwargs:
            identity = kwargs.pop("identity")
            payload = kwargs.pop("payload")
            username = kwargs.pop("username", None)
            if kwargs:
                unexpected = ", ".join(sorted(kwargs))
                raise TypeError(f"SubscribeOper.add 收到未知参数：{unexpected}")
            return super().add(
                identity=identity,
                payload=payload,
                username=username,
            )
        return add_subscribe(
            mediainfo=mediainfo,
            subscribe_oper=self,
            **kwargs,
        )

    async def async_add(
            self,
            mediainfo: Optional[MediaInfo | MusicInfo] = None,
            **kwargs: Any,
    ):
        """
        异步兼容旧订阅写入；新字典签名直接交给 canonical Oper。

        :param mediainfo: 旧调用传入的媒体识别结果
        :param kwargs: 旧订阅配置，或新 Oper 的 identity/payload/username
        :return: 订阅 ID 与结果说明
        """
        if mediainfo is None and "identity" in kwargs and "payload" in kwargs:
            identity = kwargs.pop("identity")
            payload = kwargs.pop("payload")
            username = kwargs.pop("username", None)
            if kwargs:
                unexpected = ", ".join(sorted(kwargs))
                raise TypeError(f"SubscribeOper.async_add 收到未知参数：{unexpected}")
            return await super().async_add(
                identity=identity,
                payload=payload,
                username=username,
            )
        return await async_add_subscribe(
            mediainfo=mediainfo,
            subscribe_oper=self,
            **kwargs,
        )


__all__ = ["SubscribeOper"]
