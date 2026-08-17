"""添加订阅工具"""

from typing import List, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.subscribe import SubscribeChain
from app.db.oper.user import UserOper
from app.runtime.log import logger
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaSource, MediaType, NotificationChannel
from app.domain.media import normalize_music_type


class AddSubscribeInput(BaseModel):
    """添加订阅工具的输入参数模型"""

    title: str = Field(
        ...,
        description="The title of the media to subscribe to (e.g., 'The Matrix', 'Breaking Bad')",
    )
    year: Optional[str] = Field(
        None,
        description="Release year. Recommended for video; optional when a stable music ID is provided",
    )
    media_type: str = Field(..., description="Allowed values: movie, tv, music")
    music_type: Optional[str] = Field(
        None,
        description="Required for music subscriptions: recording for one track or album for the complete album. Artists cannot be subscribed",
    )
    season: Optional[int] = Field(
        None,
        description=(
            "Season number for TV shows (optional). If omitted, the subscription defaults to season 1 only. "
            "To subscribe multiple seasons or the full series, call this tool separately for each season."
        ),
    )
    media_source: Optional[MediaSource] = Field(
        None,
        description="Media metadata source returned by search_media",
    )
    media_id: Optional[str] = Field(None, description="Native ID for media_source")
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
    audio_quality: Optional[str] = Field(
        None,
        description="Music quality tier filter: hires, lossless, lossy, or a regular-expression combination",
    )
    audio_format: Optional[str] = Field(
        None,
        description="Music audio-format filter as a regular expression, e.g. FLAC|ALAC|DSD",
    )
    min_bitrate: Optional[int] = Field(None, description="Minimum music bitrate in bits per second")
    min_bit_depth: Optional[int] = Field(None, description="Minimum music bit depth")
    min_sample_rate: Optional[int] = Field(None, description="Minimum music sample rate in Hz")
    best_version: Optional[int] = Field(
        None,
        description="Enable quality upgrades: 0 for no, 1 for yes. Music upgrades use normalized audio quality",
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
    """添加电影、电视剧、单曲或整张专辑订阅。"""

    name: str = "add_subscribe"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Subscription,
        ToolTag.Media,
    ]
    description: str = (
        "Add automated subscriptions for movies, TV shows, one music recording, or one complete album. "
        "Music requires the source-native ID and music_type='recording' or 'album'; artists are browse-only. "
        "Album subscriptions only complete after a resource is confirmed to cover the album's expected track count. "
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
        music_type = kwargs.get("music_type")
        season = kwargs.get("season")

        message = f"添加订阅: {title}"
        if year:
            message += f" ({year})"
        if media_type:
            message += f" [{media_type}]"
        if music_type:
            message += f" [{music_type}]"
        if season is not None:
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
            channel = NotificationChannel(self._channel)
        except ValueError:
            return resolved_username

        binding_keys = {
            NotificationChannel.Telegram: ("telegram_userid",),
            NotificationChannel.Discord: ("discord_userid",),
            NotificationChannel.Wechat: ("wechat_userid",),
            NotificationChannel.Feishu: ("feishu_userid", "feishu_openid"),
            NotificationChannel.WechatClawBot: ("wechatclawbot_userid",),
            NotificationChannel.Slack: ("slack_userid",),
            NotificationChannel.VoceChat: ("vocechat_userid",),
            NotificationChannel.SynologyChat: ("synologychat_userid",),
            NotificationChannel.QQ: ("qq_userid", "qq_openid"),
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
        media_type: str,
        year: Optional[str] = None,
        music_type: Optional[str] = None,
        season: Optional[int] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        start_episode: Optional[int] = None,
        total_episode: Optional[int] = None,
        quality: Optional[str] = None,
        resolution: Optional[str] = None,
        effect: Optional[str] = None,
        audio_quality: Optional[str] = None,
        audio_format: Optional[str] = None,
        min_bitrate: Optional[int] = None,
        min_bit_depth: Optional[int] = None,
        min_sample_rate: Optional[int] = None,
        best_version: Optional[int] = None,
        filter_groups: Optional[List[str]] = None,
        sites: Optional[List[int]] = None,
        **kwargs,
    ) -> str:
        """校验实体语义后调用订阅链添加精确订阅。"""
        logger.info(
            f"执行工具: {self.name}, 参数: title={title}, year={year}, media_type={media_type}, "
            f"music_type={music_type}, season={season}, media_source={media_source}, "
            f"media_id={media_id}, "
            f"start_episode={start_episode}, "
            f"total_episode={total_episode}, quality={quality}, resolution={resolution}, "
            f"effect={effect}, filter_groups={filter_groups}, sites={sites}"
        )

        try:
            subscribe_chain = SubscribeChain()
            media_type_enum = MediaType.from_agent(media_type)
            if not media_type_enum:
                return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv', 'music'"

            normalized_music_type = None
            if media_type_enum == MediaType.MUSIC:
                normalized_music_type = normalize_music_type(
                    music_type,
                    allow_artist=False,
                )
                if not normalized_music_type:
                    return (
                        "错误：音乐订阅必须提供 music_type='recording'（单曲）"
                        "或 music_type='album'（整张专辑），艺术家不能订阅"
                    )
                if not media_source or not media_id:
                    return "错误：音乐订阅必须同时提供 search_media 返回的 media_source 和 media_id"
                if any(value is not None for value in (season, start_episode, total_episode)):
                    return "错误：音乐订阅没有季集参数，不能传入 season、start_episode 或 total_episode"
            elif music_type:
                return "错误：music_type 仅能与 media_type='music' 一起使用"
            if (media_source is None) != (media_id is None):
                return "错误：media_source 和 media_id 必须同时提供"
            audio_filter_values = (
                audio_quality, audio_format, min_bitrate, min_bit_depth, min_sample_rate
            )
            if media_type_enum != MediaType.MUSIC and any(
                    value is not None for value in audio_filter_values
            ):
                return "错误：audio_quality、audio_format 和音频技术参数仅用于音乐订阅"
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
            if normalized_music_type:
                subscribe_kwargs["music_type"] = normalized_music_type
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
            if audio_quality:
                subscribe_kwargs["audio_quality"] = audio_quality
            if audio_format:
                subscribe_kwargs["audio_format"] = audio_format
            if min_bitrate is not None:
                subscribe_kwargs["min_bitrate"] = min_bitrate
            if min_bit_depth is not None:
                subscribe_kwargs["min_bit_depth"] = min_bit_depth
            if min_sample_rate is not None:
                subscribe_kwargs["min_sample_rate"] = min_sample_rate
            if best_version is not None:
                subscribe_kwargs["best_version"] = best_version
            if filter_groups:
                subscribe_kwargs["filter_groups"] = filter_groups
            if sites:
                subscribe_kwargs["sites"] = sites

            sid, message = await subscribe_chain.async_add(
                mtype=media_type_enum,
                title=title,
                year=year or "",
                media_source=media_source,
                media_id=media_id,
                season=season,
                username=subscribe_username,
                **subscribe_kwargs,
            )
            if sid:
                display_year = f" ({year})" if year else ""
                music_label = (
                    "专辑" if normalized_music_type == MUSIC_ENTITY_ALBUM else "单曲"
                ) if normalized_music_type else ""
                if message and "已存在" in message:
                    result_msg = f"{music_label}订阅已存在：{title}{display_year}"
                    if effective_season is not None:
                        result_msg += f" 第{effective_season}季"
                    result_msg += "。如需修改参数请先删除旧订阅。"
                    return result_msg

                result_msg = f"成功添加{music_label}订阅：{title}{display_year}"
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
                    if audio_quality:
                        params.append(f"音质等级: {audio_quality}")
                    if audio_format:
                        params.append(f"音频格式: {audio_format}")
                    if min_bitrate is not None:
                        params.append(f"最低码率: {round(min_bitrate / 1000)}kbps")
                    if min_bit_depth is not None:
                        params.append(f"最低位深: {min_bit_depth}bit")
                    if min_sample_rate is not None:
                        params.append(f"最低采样率: {min_sample_rate / 1000:g}kHz")
                    if best_version is not None:
                        params.append(f"音质洗版: {'开启' if best_version else '关闭'}")
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
