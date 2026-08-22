"""删除订阅工具"""

from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.subscription.delete import (
    SubscribeDeletionActor,
    get_delete_subscribe_scope,
)
from app.application.subscription.mutation import (
    SubscriptionActor,
    get_subscription_mutation_scope,
)
from app.runtime.log import logger


class DeleteSubscribeInput(BaseModel):
    """删除订阅工具的输入参数模型"""

    subscribe_id: int = Field(
        ...,
        description="The ID of the subscription to delete (can be obtained from query_subscribes tool)",
    )


class DeleteSubscribeTool(MoviePilotTool):
    """按订阅 ID 删除影视、单曲或专辑订阅。"""

    name: str = "delete_subscribe"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Subscription,
        ToolTag.Admin,
    ]
    description: str = "Delete a media subscription by its ID. This will remove the subscription and stop automatic downloads for that media."
    args_schema: Type[BaseModel] = DeleteSubscribeInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据删除参数生成友好的提示消息"""
        subscribe_id = kwargs.get("subscribe_id")
        return f"删除订阅 (ID: {subscribe_id})"

    async def run(self, subscribe_id: int, **kwargs) -> str:
        """删除订阅并同步刷新带音乐实体维度的共享统计。"""
        logger.info(f"执行工具: {self.name}, 参数: subscribe_id={subscribe_id}")

        try:
            async with get_subscription_mutation_scope() as mutation:
                subscribe = await mutation.get_accessible(
                    subscribe_id,
                    SubscriptionActor(name="agent", is_superuser=True),
                )
            if not subscribe:
                return f"订阅 ID {subscribe_id} 不存在"

            async with get_delete_subscribe_scope() as command:
                deleted = await command.execute(
                    subscribe_id,
                    SubscribeDeletionActor(username="agent", is_superuser=True),
                )
            if not deleted:
                return f"订阅 ID {subscribe_id} 不存在"

            return f"成功删除订阅：{subscribe.name} ({subscribe.year})"
        except Exception as e:
            logger.error(f"删除订阅失败: {e}", exc_info=True)
            return f"删除订阅时发生错误: {str(e)}"
