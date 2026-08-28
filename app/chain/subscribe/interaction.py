"""订阅交互与删除入口编排"""

from typing import Optional, Union, cast

from app.application.messaging.subscribe import SubscribeInteractionHandler
from app.application.subscription.delete import (
    SubscribeDeletionActor,
)
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.schemas.message import Message as _SchemaMessage
from app.schemas.types import (
    NotificationChannel,
)


class SubscribeInteractionOwner(_SubscribeOwnerBase):
    """订阅交互与删除入口编排，作为 SubscribeChain 的单一职责实现 owner。"""

    def _interaction_handler(self) -> "SubscribeInteractionHandler":
        """构造 /subscribes 交互处理器，业务动作由本链提供。"""
        return SubscribeInteractionHandler(
            messenger=self,
            actions=self,
            repository=self.subscription_repository,
            delete_subscription=self._delete_subscription,
        )

    def _delete_subscription(self, subscribe_id: int) -> bool:
        """通过统一同步命令删除订阅，保留消息入口原有的全局管理权限。"""
        with self.sync_subscription_delete_scope() as command:
            return cast(
                bool,
                command.execute(
                    subscribe_id,
                    SubscribeDeletionActor(username="", is_superuser=True),
                ),
            )

    def remote_delete(
        self,
        arg_str: str,
        channel: NotificationChannel,
        userid: Optional[Union[str, int]] = None,
        source: Optional[str] = None,
    ) -> None:
        """
        删除订阅
        """
        if not arg_str:
            self.post_message(
                _SchemaMessage(
                    channel=channel,
                    source=source,
                    title="请输入正确的命令格式：/subscribe_delete [id]，[id]为订阅编号",
                    userid=userid,
                    save_history=False,
                )
            )
            return
        arg_strs = str(arg_str).split()
        for arg_str in arg_strs:
            arg_str = arg_str.strip()
            if not arg_str.isdigit():
                continue
            subscribe_id = int(arg_str)
            if not self._delete_subscription(subscribe_id):
                self.post_message(
                    _SchemaMessage(
                        channel=channel,
                        source=source,
                        title=f"订阅编号 {subscribe_id} 不存在！",
                        userid=userid,
                        save_history=False,
                    )
                )
                return
        # 重新发送消息
        self.remote_list(channel=channel, userid=userid, source=source)
