"""查询媒体详情工具"""

import asyncio
import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.media import MediaChain
from app.runtime.log import logger
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_ARTIST,
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
)
from app.domain.media import normalize_music_type
from ._music_utils import (
    simplify_music_album,
    simplify_music_artist,
    simplify_music_info,
)

DIRECTOR_PREVIEW_LIMIT = 10
ACTOR_PREVIEW_LIMIT = 20
SEASON_PREVIEW_LIMIT = 100


class QueryMediaDetailInput(BaseModel):
    """查询媒体详情工具的输入参数模型"""
    media_source: MediaSource = Field(..., description="Media metadata source")
    media_id: str = Field(..., description="Native ID for media_source")
    media_type: str = Field(..., description="Allowed values: movie, tv, music")
    music_type: Optional[str] = Field(
        None,
        description="Required for music: recording, album, or artist",
    )
    include_artist_albums: Optional[bool] = Field(
        False,
        description="For artist detail, include a paged album/EP/single catalog preview",
    )
    include_related_artists: Optional[bool] = Field(
        False,
        description="For artist detail, include related artists",
    )
    page: Optional[int] = Field(1, description="Artist album catalog page, default 1")
    count: Optional[int] = Field(20, description="Artist catalog/related result count, max 30")
    album_type: Optional[str] = Field(
        None,
        description="Optional artist catalog filter: album, single, ep, broadcast, other, compilation, soundtrack, live, or remix",
    )


class QueryMediaDetailTool(MoviePilotTool):
    """按稳定媒体身份查询影视、单曲、专辑或艺术家详情。"""

    name: str = "query_media_detail"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Media,
    ]
    description: str = (
        "Query details by stable metadata ID for movies, TV, music recordings, albums, or artists. "
        "Music requires media_source, media_id, and music_type. Album details include a bounded track list; "
        "artist details may optionally include catalog and related-artist previews. Artists are browse-only."
    )
    args_schema: Type[BaseModel] = QueryMediaDetailInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        return (
            f"查询媒体详情: {kwargs.get('media_source') or '媒体源'} "
            f"ID {kwargs.get('media_id')}"
        )

    async def run(
            self, media_type: str, media_source: MediaSource,
            media_id: str, music_type: Optional[str] = None,
            include_artist_albums: Optional[bool] = False,
            include_related_artists: Optional[bool] = False,
            page: Optional[int] = 1, count: Optional[int] = 20,
            album_type: Optional[str] = None, **kwargs,
    ) -> str:
        """执行媒体详情查询，并限制音乐目录型结果的上下文大小。"""
        logger.info(
            f"执行工具: {self.name}, 参数: media_type={media_type}, "
            f"music_type={music_type}, media_source={media_source}, "
            f"media_id={media_id}"
        )

        if not str(media_id or "").strip():
            return json.dumps({
                "success": False,
                "message": "必须提供 media_id"
            }, ensure_ascii=False)

        try:
            media_type_enum = MediaType.from_agent(media_type)
            if not media_type_enum:
                return json.dumps({
                    "success": False,
                    "message": (
                        f"无效的媒体类型 '{media_type}'，"
                        "支持的类型：'movie', 'tv', 'music'"
                    )
                }, ensure_ascii=False)

            if media_type_enum == MediaType.MUSIC:
                normalized_music_type = normalize_music_type(music_type)
                if not normalized_music_type:
                    return json.dumps({
                        "success": False,
                        "message": (
                            "查询音乐详情必须提供 music_type："
                            "'recording', 'album' 或 'artist'"
                        ),
                    }, ensure_ascii=False)
                if not media_source or not media_id:
                    return json.dumps({
                        "success": False,
                        "message": "查询音乐详情必须同时提供 media_source 和 media_id",
                    }, ensure_ascii=False)

                media_chain = MediaChain()
                if normalized_music_type == MUSIC_ENTITY_ALBUM:
                    album_info = await media_chain.async_get_music_album(
                        media_source, media_id
                    )
                    if not album_info:
                        return json.dumps({
                            "success": False,
                            "message": f"未找到专辑 {media_source}:{media_id}",
                        }, ensure_ascii=False)
                    return json.dumps(
                        simplify_music_album(album_info),
                        ensure_ascii=False,
                        indent=2,
                    )

                if normalized_music_type == MUSIC_ENTITY_ARTIST:
                    artist_info = await media_chain.async_get_music_artist(
                        media_source, media_id
                    )
                    if not artist_info:
                        return json.dumps({
                            "success": False,
                            "message": f"未找到艺术家 {media_source}:{media_id}",
                        }, ensure_ascii=False)
                    normalized_page = max(1, page or 1)
                    normalized_count = max(1, min(count or 20, 30))
                    result = simplify_music_artist(artist_info)
                    pending = []
                    if include_artist_albums:
                        pending.append((
                            "albums",
                            media_chain.async_get_music_artist_albums(
                                media_source=media_source,
                                media_id=media_id,
                                page=normalized_page,
                                count=normalized_count,
                                album_type=album_type,
                            ),
                        ))
                    if include_related_artists:
                        pending.append((
                            "related_artists",
                            media_chain.async_get_music_artist_related(
                                media_source=media_source,
                                media_id=media_id,
                                count=normalized_count,
                            ),
                        ))
                    if pending:
                        values = await asyncio.gather(*(call for _, call in pending))
                        for (key, _), items in zip(pending, values):
                            result[key] = [
                                simplify_music_info(item)
                                if key == "albums"
                                else simplify_music_artist(item)
                                for item in items
                            ]
                    return json.dumps(result, ensure_ascii=False, indent=2)

                mediainfo = await media_chain.async_recognize_media(
                    media_source=media_source,
                    media_id=media_id,
                    mtype=MediaType.MUSIC,
                    music_type=normalized_music_type,
                )
                if (
                    not mediainfo
                    or getattr(mediainfo, "music_type", MUSIC_ENTITY_RECORDING)
                    != MUSIC_ENTITY_RECORDING
                ):
                    return json.dumps({
                        "success": False,
                        "message": f"未找到单曲 {media_source}:{media_id}",
                    }, ensure_ascii=False)
                return json.dumps(
                    simplify_music_info(mediainfo),
                    ensure_ascii=False,
                    indent=2,
                )

            media_chain = MediaChain()
            mediainfo = await media_chain.async_recognize_media(
                media_source=media_source,
                media_id=media_id,
                mtype=media_type_enum,
            )

            if not mediainfo:
                return json.dumps({
                    "success": False,
                    "message": f"未找到 {media_source} ID {media_id} 的媒体信息"
                }, ensure_ascii=False)

            # 精简 genres - 只保留名称
            genres = [g.get("name") for g in (mediainfo.genres or []) if g.get("name")]

            # 精简 directors - 只保留姓名和职位
            director_source = [d for d in (mediainfo.directors or []) if d.get("name")]
            directors = [
                {
                    "name": d.get("name"),
                    "job": d.get("job")
                }
                for d in director_source[:DIRECTOR_PREVIEW_LIMIT]
            ]

            # 精简 actors - 只保留姓名和角色
            actor_source = [a for a in (mediainfo.actors or []) if a.get("name")]
            actors = [
                {
                    "name": a.get("name"),
                    "character": a.get("character")
                }
                for a in actor_source[:ACTOR_PREVIEW_LIMIT]
            ]

            # 构建基础媒体详情信息
            result = {
                "status": mediainfo.status,
                "genres": genres,
                "directors": directors,
                "directors_total": len(director_source),
                "directors_truncated": len(director_source) > DIRECTOR_PREVIEW_LIMIT,
                "actors": actors,
                "actors_total": len(actor_source),
                "actors_truncated": len(actor_source) > ACTOR_PREVIEW_LIMIT,
            }

            # 如果是电视剧，添加电视剧特有信息
            if mediainfo.type == MediaType.TV:
                # 精简 season_info - 只保留基础摘要
                season_source = [
                    s for s in (mediainfo.season_info or [])
                    if s.get("season_number") is not None
                ]
                season_info = [
                    {
                        "season_number": s.get("season_number"),
                        "name": s.get("name"),
                        "episode_count": s.get("episode_count"),
                        "air_date": s.get("air_date")
                    }
                    for s in season_source[:SEASON_PREVIEW_LIMIT]
                ]

                result.update({
                    "number_of_seasons": mediainfo.number_of_seasons,
                    "number_of_episodes": mediainfo.number_of_episodes,
                    "first_air_date": mediainfo.first_air_date,
                    "last_air_date": mediainfo.last_air_date,
                    "season_info": season_info,
                    "season_info_total": len(season_source),
                    "season_info_truncated": len(season_source) > SEASON_PREVIEW_LIMIT,
                })

            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            error_message = f"查询媒体详情失败: {str(e)}"
            logger.error(f"查询媒体详情失败: {e}", exc_info=True)
            return json.dumps({
                "success": False,
                "message": error_message,
                "media_source": media_source,
                "media_id": media_id,
            }, ensure_ascii=False)
