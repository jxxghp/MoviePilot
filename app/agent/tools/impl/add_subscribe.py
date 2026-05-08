"""添加订阅工具"""

from typing import List, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.chain.subscribe import SubscribeChain
from app.db.user_oper import UserOper
from app.log import logger
from app.schemas.types import MediaType, MessageChannel


class AddSubscribeInput(BaseModel):
    """添加订阅工具的输入参数模型"""

    explanation: str = Field(
        ...,
        description="Clear explanation of why this tool is being used in the current context",
    )
    title: str = Field(
        ...,
        description="The title of the media to subscribe to (e.g., 'The Matrix', 'Breaking Bad')",
    )
    year: str = Field(
        ...,
        description="Release year of the media (required for accurate identification)",
    )
    media_type: str = Field(..., description="Allowed values: movie, tv")
    season: Optional[int] = Field(
        None,
        description=(
            "Season number for TV shows (optional). If omitted, the subscription defaults to season 1 only. "
            "To subscribe multiple seasons or the full series, call this tool separately for each season."
        ),
    )
    tmdb_id: Optional[int] = Field(
        None,
        description="TMDB database ID for precise media identification (optional, can be obtained from search_media tool)",
    )
    douban_id: Optional[str] = Field(
        None,
        description="Douban ID for precise media identification (optional, alternative to tmdb_id)",
    )
    start_episode: Optional[int] = Field(
        None,
        description="Starting episode number for TV shows (optional, defaults to 1 if not specified)",
    )
    total_episode: Optional[int] = Field(
        None,
        description="Total number of episodes for TV shows (optional, will be auto-detected from TMDB if not specified)",
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
    filter_groups: Optional[List[str]] = Field(
        None,
        description="List of filter rule group names to apply (optional, can be obtained from query_rule_groups tool)",
    )
    sites: Optional[List[int]] = Field(
        None,
        description="List of site IDs to search from (optional, can be obtained from query_sites tool)",
    )


class AddSubscribeTool(MoviePilotTool):
    name: str = "add_subscribe"
    description: str = (
        "Add media subscription to create automated download rules for movies and TV shows. "
        "The system will automatically search and download new episodes or releases based on the subscription criteria. "
        "For TV shows, omitting `season` subscribes season 1 only by default; to subscribe multiple seasons or "
        "the full series, call this tool once per season. Supports advanced filtering options like quality, "
        "resolution, and effect filters using regular expressions."
    )
    args_schema: Type[BaseModel] = AddSubscribeInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据订阅参数生成友好的提示消息"""
        title = kwargs.get("title", "")
        year = kwargs.get("year", "")
        media_type = kwargs.get("media_type", "")
        season = kwargs.get("season")

        message = f"添加订阅: {title}"
        if year:
            message += f" ({year})"
        if media_type:
            message += f" [{media_type}]"
        if season:
            message += f" 第{season}季"
        elif media_type == "tv":
            message += " 第1季(默认)"

        return message

    async def _resolve_subscribe_username(self) -> Optional[str]:
        """优先映射为系统用户名，未绑定时回退当前渠道用户名。"""
        resolved_username = self._username
        if not self._channel or not self._user_id:
            return resolved_username

        try:
            channel = MessageChannel(self._channel)
        except ValueError:
            return resolved_username

        binding_keys = {
            MessageChannel.Telegram: ("telegram_userid",),
            MessageChannel.Discord: ("discord_userid",),
            MessageChannel.Wechat: ("wechat_userid",),
            MessageChannel.Slack: ("slack_userid",),
            MessageChannel.VoceChat: ("vocechat_userid",),
            MessageChannel.SynologyChat: ("synologychat_userid",),
            MessageChannel.QQ: ("qq_userid", "qq_openid"),
        }.get(channel)
        if not binding_keys:
            return resolved_username

        mapped_username = await self.run_blocking(
            "db",
            UserOper().get_name,
            **{key: self._user_id for key in binding_keys},
        )
        return mapped_username or resolved_username

    async def run(
        self,
        title: str,
        year: str,
        media_type: str,
        season: Optional[int] = None,
        tmdb_id: Optional[int] = None,
        douban_id: Optional[str] = None,
        start_episode: Optional[int] = None,
        total_episode: Optional[int] = None,
        quality: Optional[str] = None,
        resolution: Optional[str] = None,
        effect: Optional[str] = None,
        filter_groups: Optional[List[str]] = None,
        sites: Optional[List[int]] = None,
        **kwargs,
    ) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: title={title}, year={year}, media_type={media_type}, "
            f"season={season}, tmdb_id={tmdb_id}, douban_id={douban_id}, start_episode={start_episode}, "
            f"total_episode={total_episode}, quality={quality}, resolution={resolution}, "
            f"effect={effect}, filter_groups={filter_groups}, sites={sites}"
        )

        try:
            subscribe_chain = SubscribeChain()
            media_type_enum = MediaType.from_agent(media_type)
            if not media_type_enum:
                return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv'"
            effective_season = (
                season
                if season is not None
                else 1
                if media_type_enum == MediaType.TV
                else None
            )
            subscribe_username = await self._resolve_subscribe_username()

            # 构建额外的订阅参数
            subscribe_kwargs = {}
            if start_episode is not None:
                subscribe_kwargs["start_episode"] = start_episode
            if total_episode is not None:
                subscribe_kwargs["total_episode"] = total_episode
            if quality:
                subscribe_kwargs["quality"] = quality
            if resolution:
                subscribe_kwargs["resolution"] = resolution
            if effect:
                subscribe_kwargs["effect"] = effect
            if filter_groups:
                subscribe_kwargs["filter_groups"] = filter_groups
            if sites:
                subscribe_kwargs["sites"] = sites

            sid, message = await subscribe_chain.async_add(
                mtype=media_type_enum,
                title=title,
                year=year,
                tmdbid=tmdb_id,
                doubanid=douban_id,
                season=season,
                username=subscribe_username,
                **subscribe_kwargs,
            )
            if sid:
                if message and "已存在" in message:
                    result_msg = f"订阅已存在：{title} ({year})"
                    if effective_season is not None:
                        result_msg += f" 第{effective_season}季"
                    result_msg += "。如需修改参数请先删除旧订阅。"
                    return result_msg

                result_msg = f"成功添加订阅：{title} ({year})"
                if effective_season is not None:
                    result_msg += f" 第{effective_season}季"
                    if season is None:
                        result_msg += "（未指定季号，默认按第一季订阅）"
                if subscribe_kwargs:
                    params = []
                    if start_episode is not None:
                        params.append(f"开始集数: {start_episode}")
                    if total_episode is not None:
                        params.append(f"总集数: {total_episode}")
                    if quality:
                        params.append(f"质量过滤: {quality}")
                    if resolution:
                        params.append(f"分辨率过滤: {resolution}")
                    if effect:
                        params.append(f"特效过滤: {effect}")
                    if filter_groups:
                        params.append(f"规则组: {', '.join(filter_groups)}")
                    if sites:
                        params.append(f"站点: {', '.join(map(str, sites))}")
                    if params:
                        result_msg += f"\n配置参数: {', '.join(params)}"
                return result_msg
            else:
                return f"添加订阅失败：{message}"
        except Exception as e:
            logger.error(f"添加订阅失败: {e}", exc_info=True)
            return f"添加订阅时发生错误: {str(e)}"
