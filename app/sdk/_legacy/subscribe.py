"""把旧订阅 Oper 写入调用转交给新的应用服务。"""

from typing import Any, Optional, cast

from app.application.subscription.write import (
    SubscribeWriter,
    add_subscribe,
    async_add_subscribe,
)
from app.db.oper.subscribe import SubscribeOper as CanonicalSubscribeOper
from app.domain.context import MediaInfo, MusicInfo


class SubscribeOper(CanonicalSubscribeOper):
    """保留旧 ``mediainfo`` 写入签名，同时继承新的查询接口。"""

    # 旧插件 ABI 以 mediainfo 为首参，故签名有意宽于 canonical Oper。
    def add(  # type: ignore[override]
            self,
            mediainfo: Optional[MediaInfo | MusicInfo] = None,
            **kwargs: Any,
    ) -> tuple[int, str]:
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
            mediainfo=cast(MediaInfo | MusicInfo, mediainfo),
            subscribe_oper=cast(SubscribeWriter, self),
            **kwargs,
        )

    # 异步入口保留相同的旧插件双签名合同。
    async def async_add(  # type: ignore[override]
            self,
            mediainfo: Optional[MediaInfo | MusicInfo] = None,
            **kwargs: Any,
    ) -> tuple[int, str]:
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
            mediainfo=cast(MediaInfo | MusicInfo, mediainfo),
            subscribe_oper=cast(SubscribeWriter, self),
            **kwargs,
        )


__all__ = ["SubscribeOper"]
