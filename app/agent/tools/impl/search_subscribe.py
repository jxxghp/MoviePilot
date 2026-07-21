"""搜索订阅缺失剧集工具"""

import json
from typing import Optional, Type, List

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.subscribe import SubscribeChain
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import media_type_to_agent


class SearchSubscribeInput(BaseModel):
    """搜索订阅缺失剧集工具的输入参数模型"""
    subscribe_id: int = Field(..., description="The ID of the subscription to search for missing episodes (can be obtained from query_subscribes tool)")
    manual: Optional[bool] = Field(False, description="Whether this is a manual search (default: False)")
    filter_groups: Optional[List[str]] = Field(None,
                                               description="List of filter rule group names to apply for this search (optional, can be obtained from query_rule_groups tool. If provided, will temporarily update the subscription's filter groups before searching)")


class SearchSubscribeTool(MoviePilotTool):
    name: str = "search_subscribe"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Write,
        ToolTag.Subscription,
        ToolTag.Resource,
    ]
    description: str = "Search for missing episodes/resources for a specific subscription. This tool will search torrent sites for the missing episodes of the subscription and automatically download matching resources. Use this when a user wants to search for missing episodes of a specific subscription."
    args_schema: Type[BaseModel] = SearchSubscribeInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据搜索参数生成友好的提示消息"""
        subscribe_id = kwargs.get("subscribe_id")
        manual = kwargs.get("manual", False)

        message = f"搜索订阅 #{subscribe_id} 的缺失剧集"
        if manual:
            message += "（手动搜索）"

        return message

    async def run(self, subscribe_id: int, manual: Optional[bool] = False,
                  filter_groups: Optional[List[str]] = None, **kwargs) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: subscribe_id={subscribe_id}, manual={manual}, filter_groups={filter_groups}")

        try:
            # 先验证订阅是否存在
            subscribe_oper = SubscribeOper()
            subscribe = subscribe_oper.get(subscribe_id)

            if not subscribe:
                return json.dumps({
                    "success": False,
                    "message": f"订阅不存在: {subscribe_id}"
                }, ensure_ascii=False)

            # 获取订阅信息用于返回
            subscribe_info = {
                "id": subscribe.id,
                "name": subscribe.name,
                "year": subscribe.year,
                "type": media_type_to_agent(subscribe.type),
                "season": subscribe.season,
                "state": subscribe.state,
                "total_episode": subscribe.total_episode,
                "lack_episode": subscribe.lack_episode,
                "tmdbid": subscribe.tmdbid,
                "doubanid": subscribe.doubanid,
                "bangumiid": subscribe.bangumiid,
                "anilistid": subscribe.anilistid,
                "media_source": subscribe.media_source,
                "media_id": subscribe.media_id,
            }

            # 检查订阅状态
            if subscribe.state == "S":
                return json.dumps({
                    "success": False,
                    "message": f"订阅 #{subscribe_id} ({subscribe.name}) 已暂停，无法搜索",
                    "subscribe": subscribe_info
                }, ensure_ascii=False)

            # 如果提供了 filter_groups 参数，先更新订阅的规则组
            if filter_groups is not None:
                subscribe_oper.update(subscribe_id, {"filter_groups": filter_groups})
                logger.info(f"更新订阅 #{subscribe_id} 的规则组为: {filter_groups}")

            # 订阅搜索会触发大量同步站点访问，统一走 subscribe 线程池。
            await self.run_blocking(
                "subscribe",
                SubscribeChain().search,
                sid=subscribe_id,
                state="R",  # 当 sid 有值时此参数会被忽略
                manual=manual,
            )

            # 重新获取订阅信息以获取更新后的状态
            updated_subscribe = subscribe_oper.get(subscribe_id)
            if updated_subscribe:
                subscribe_info.update({
                    "state": updated_subscribe.state,
                    "lack_episode": updated_subscribe.lack_episode,
                    "last_update": updated_subscribe.last_update,
                    "filter_groups": updated_subscribe.filter_groups
                })

            # 如果提供了规则组，会在返回信息中显示
            result = {
                "success": True,
                "message": f"订阅 #{subscribe_id} ({subscribe.name}) 搜索完成",
                "subscribe": subscribe_info
            }

            if filter_groups is not None:
                result["message"] += f"（已应用规则组: {', '.join(filter_groups)}）"

            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            error_message = f"搜索订阅缺失剧集失败: {str(e)}"
            logger.error(f"搜索订阅缺失剧集失败: {e}", exc_info=True)
            return json.dumps({
                "success": False,
                "message": error_message,
                "subscribe_id": subscribe_id
            }, ensure_ascii=False)
