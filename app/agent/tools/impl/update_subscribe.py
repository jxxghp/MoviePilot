"""更新订阅工具"""

import json
from typing import Optional, Type, List

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.core.event import eventmanager
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.event import SubscribeModifiedEventData
from app.schemas.types import EventType


class UpdateSubscribeInput(BaseModel):
    """更新订阅工具的输入参数模型"""

    subscribe_id: int = Field(
        ...,
        description="The ID of the subscription to update (can be obtained from query_subscribes tool)",
    )
    name: Optional[str] = Field(None, description="Subscription name/title (optional)")
    year: Optional[str] = Field(None, description="Release year (optional)")
    season: Optional[int] = Field(
        None, description="Season number for TV shows (optional)"
    )
    total_episode: Optional[int] = Field(
        None, description="Total number of episodes (optional)"
    )
    lack_episode: Optional[int] = Field(
        None, description="Number of missing episodes (optional)"
    )
    start_episode: Optional[int] = Field(
        None, description="Starting episode number (optional)"
    )
    quality: Optional[str] = Field(
        None,
        description="Quality filter as regular expression (optional, e.g., 'BluRay|WEB-DL|HDTV')",
    )
    resolution: Optional[str] = Field(
        None,
        description="Resolution filter as regular expression (optional, e.g., '1080p|720p|2160p')",
    )
    effect: Optional[str] = Field(
        None,
        description="Effect filter as regular expression (optional, e.g., 'HDR|DV|SDR')",
    )
    include: Optional[str] = Field(
        None, description="Include filter as regular expression (optional)"
    )
    exclude: Optional[str] = Field(
        None, description="Exclude filter as regular expression (optional)"
    )
    filter: Optional[str] = Field(
        None, description="Filter rule as regular expression (optional)"
    )
    state: Optional[str] = Field(
        None,
        description="Subscription state: 'R' for enabled, 'P' for pending, 'S' for paused (optional)",
    )
    sites: Optional[List[int]] = Field(
        None, description="List of site IDs to search from (optional)"
    )
    downloader: Optional[str] = Field(None, description="Downloader name (optional)")
    save_path: Optional[str] = Field(
        None, description="Save path for downloaded files (optional)"
    )
    best_version: Optional[int] = Field(
        None,
        description="Whether to upgrade to best version: 0 for no, 1 for yes (optional)",
    )
    best_version_full: Optional[int] = Field(
        None,
        description="For TV best-version subscriptions, only download full-season packs: 0 for no, 1 for yes (optional)",
    )
    custom_words: Optional[str] = Field(
        None, description="Custom recognition words (optional)"
    )
    media_category: Optional[str] = Field(
        None, description="Custom media category (optional)"
    )
    episode_group: Optional[str] = Field(
        None, description="Episode group ID (optional)"
    )


class UpdateSubscribeTool(MoviePilotTool):
    name: str = "update_subscribe"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Subscription,
        ToolTag.Admin,
    ]
    description: str = "Update subscription properties including filters, episode counts, state, and other settings. Supports updating quality/resolution filters, episode tracking, subscription state, and download configuration."
    args_schema: Type[BaseModel] = UpdateSubscribeInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据更新参数生成友好的提示消息"""
        subscribe_id = kwargs.get("subscribe_id")
        fields_updated = []

        if kwargs.get("name"):
            fields_updated.append("名称")
        if kwargs.get("total_episode") is not None:
            fields_updated.append("总集数")
        if kwargs.get("lack_episode") is not None:
            fields_updated.append("缺失集数")
        if kwargs.get("quality"):
            fields_updated.append("质量过滤")
        if kwargs.get("resolution"):
            fields_updated.append("分辨率过滤")
        if kwargs.get("state"):
            state_map = {"R": "启用", "P": "禁用", "S": "暂停"}
            fields_updated.append(
                f"状态({state_map.get(kwargs.get('state'), kwargs.get('state'))})"
            )
        if kwargs.get("sites"):
            fields_updated.append("站点")
        if kwargs.get("downloader"):
            fields_updated.append("下载器")

        if fields_updated:
            return f"更新订阅 #{subscribe_id}: {', '.join(fields_updated)}"
        return f"更新订阅 #{subscribe_id}"

    async def run(
        self,
        subscribe_id: int,
        name: Optional[str] = None,
        year: Optional[str] = None,
        season: Optional[int] = None,
        total_episode: Optional[int] = None,
        lack_episode: Optional[int] = None,
        start_episode: Optional[int] = None,
        quality: Optional[str] = None,
        resolution: Optional[str] = None,
        effect: Optional[str] = None,
        include: Optional[str] = None,
        exclude: Optional[str] = None,
        filter: Optional[str] = None,
        state: Optional[str] = None,
        sites: Optional[List[int]] = None,
        downloader: Optional[str] = None,
        save_path: Optional[str] = None,
        best_version: Optional[int] = None,
        best_version_full: Optional[int] = None,
        custom_words: Optional[str] = None,
        media_category: Optional[str] = None,
        episode_group: Optional[str] = None,
        **kwargs,
    ) -> str:
        logger.info(f"执行工具: {self.name}, 参数: subscribe_id={subscribe_id}")

        try:
            subscribe_oper = SubscribeOper()
            subscribe = await subscribe_oper.async_get(subscribe_id)
            if not subscribe:
                return json.dumps(
                    {"success": False, "message": f"订阅不存在: {subscribe_id}"},
                    ensure_ascii=False,
                )

            # 保存旧数据用于事件
            old_subscribe_dict = subscribe.to_dict()

            # 构建更新字典
            subscribe_dict = {}

            # 基本信息
            if name is not None:
                subscribe_dict["name"] = name
            if year is not None:
                subscribe_dict["year"] = year
            if season is not None:
                subscribe_dict["season"] = season

            # 集数相关
            if total_episode is not None:
                subscribe_dict["total_episode"] = total_episode
                # 如果总集数增加，缺失集数也要相应增加
                if total_episode > (subscribe.total_episode or 0):
                    old_lack = subscribe.lack_episode or 0
                    subscribe_dict["lack_episode"] = old_lack + (
                        total_episode - (subscribe.total_episode or 0)
                    )
                # 标记为手动修改过总集数
                subscribe_dict["manual_total_episode"] = 1

            # 缺失集数处理（只有在没有提供总集数时才单独处理）
            # 注意：如果 lack_episode 为 0，不更新（避免更新为0）
            if lack_episode is not None and total_episode is None:
                if lack_episode > 0:
                    subscribe_dict["lack_episode"] = lack_episode
                # 如果 lack_episode 为 0，不添加到更新字典中（保持原值或由总集数逻辑处理）

            if start_episode is not None:
                subscribe_dict["start_episode"] = start_episode

            # 过滤规则
            if quality is not None:
                subscribe_dict["quality"] = quality
            if resolution is not None:
                subscribe_dict["resolution"] = resolution
            if effect is not None:
                subscribe_dict["effect"] = effect
            if include is not None:
                subscribe_dict["include"] = include
            if exclude is not None:
                subscribe_dict["exclude"] = exclude
            if filter is not None:
                subscribe_dict["filter"] = filter

            # 状态
            if state is not None:
                valid_states = ["R", "P", "S", "N"]
                if state not in valid_states:
                    return json.dumps(
                        {
                            "success": False,
                            "message": f"无效的订阅状态: {state}，有效状态: {', '.join(valid_states)}",
                        },
                        ensure_ascii=False,
                    )
                subscribe_dict["state"] = state

            # 下载配置
            if sites is not None:
                subscribe_dict["sites"] = sites
            if downloader is not None:
                subscribe_dict["downloader"] = downloader
            if save_path is not None:
                subscribe_dict["save_path"] = save_path
            if best_version is not None:
                subscribe_dict["best_version"] = best_version
            if best_version_full is not None:
                subscribe_dict["best_version_full"] = best_version_full

            # 其他配置
            if custom_words is not None:
                subscribe_dict["custom_words"] = custom_words
            if media_category is not None:
                subscribe_dict["media_category"] = media_category
            if episode_group is not None:
                subscribe_dict["episode_group"] = episode_group

            # 如果没有要更新的字段
            if not subscribe_dict:
                return json.dumps(
                    {"success": False, "message": "没有提供要更新的字段"},
                    ensure_ascii=False,
                )

            # 更新订阅
            await subscribe_oper.async_update(subscribe_id, subscribe_dict)

            # 重新获取更新后的订阅数据
            updated_subscribe = await subscribe_oper.async_get(subscribe_id)

            # 发送订阅调整事件
            await eventmanager.async_send_event(
                EventType.SubscribeModified,
                SubscribeModifiedEventData(
                    subscribe_id=subscribe_id,
                    old_subscribe_info=old_subscribe_dict,
                    subscribe_info=updated_subscribe.to_dict()
                    if updated_subscribe
                    else {},
                    scene="agent_update",
                ).to_dict(),
            )

            # 构建返回结果
            result = {
                "success": True,
                "message": f"订阅 #{subscribe_id} 更新成功",
                "subscribe_id": subscribe_id,
                "updated_fields": list(subscribe_dict.keys()),
            }

            if updated_subscribe:
                result["subscribe"] = {
                    "id": updated_subscribe.id,
                    "name": updated_subscribe.name,
                    "year": updated_subscribe.year,
                    "type": updated_subscribe.type,
                    "season": updated_subscribe.season,
                    "state": updated_subscribe.state,
                    "total_episode": updated_subscribe.total_episode,
                    "lack_episode": updated_subscribe.lack_episode,
                    "start_episode": updated_subscribe.start_episode,
                    "quality": updated_subscribe.quality,
                    "resolution": updated_subscribe.resolution,
                    "effect": updated_subscribe.effect,
                }

            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            error_message = f"更新订阅失败: {str(e)}"
            logger.error(f"更新订阅失败: {e}", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "message": error_message,
                    "subscribe_id": subscribe_id,
                },
                ensure_ascii=False,
            )
