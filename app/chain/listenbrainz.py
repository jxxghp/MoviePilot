from typing import Any

from app.chain import ChainBase
from app.domain.context import MusicInfo
from app.schemas.types import (
    LISTENBRAINZ_CHART_RANGES,
    LISTENBRAINZ_FRESH_MAX_DAYS,
    LISTENBRAINZ_FRESH_SORTS,
    MUSIC_ENTITY_RECORDING,
    MediaSource,
)

__all__ = [
    "ListenBrainzChain",
    "LISTENBRAINZ_CHART_RANGES",
    "LISTENBRAINZ_FRESH_MAX_DAYS",
    "LISTENBRAINZ_FRESH_SORTS",
]


class ListenBrainzChain(ChainBase):
    """ListenBrainz 音乐榜单与新发行来源链。"""

    result_source = MediaSource.MusicBrainz

    def music_chart(
            self,
            range_name: str,
            page: int = 1,
            count: int = 30,
            entity: str = MUSIC_ENTITY_RECORDING,
    ) -> list[MusicInfo]:
        """分页读取 ListenBrainz 全站音乐榜单。"""
        result = self.unicast(
            "music_chart",
            range_name=range_name,
            offset=max(page - 1, 0) * max(1, count),
            count=count,
            entity=entity,
        )
        return self._music_infos(result, limit=count)

    async def async_music_chart(
            self,
            range_name: str,
            page: int = 1,
            count: int = 30,
            entity: str = MUSIC_ENTITY_RECORDING,
    ) -> list[MusicInfo]:
        """异步分页读取 ListenBrainz 全站音乐榜单。"""
        result = await self.async_unicast(
            "music_chart",
            range_name=range_name,
            offset=max(page - 1, 0) * max(1, count),
            count=count,
            entity=entity,
        )
        return self._music_infos(result, limit=count)

    async def async_music_fresh_releases(
            self,
            days: int = 14,
            sort: str = "release_date",
            past: bool = True,
            future: bool = True,
            page: int = 1,
            count: int = 30,
    ) -> list[MusicInfo]:
        """异步分页读取 ListenBrainz 官方新发行专辑。"""
        result = await self.async_unicast(
            "music_fresh_releases",
            days=days,
            sort=sort,
            past=past,
            future=future,
            offset=max(page - 1, 0) * max(1, count),
            count=count,
        )
        return self._music_infos(result, limit=count)

    @classmethod
    def _music_infos(cls, result: Any, limit: int) -> list[MusicInfo]:
        """将榜单模块结果转换为带 MusicBrainz 身份的音乐列表。"""
        candidates = result if isinstance(result, list) else []
        infos = [
            item if isinstance(item, MusicInfo) else MusicInfo.from_dict(item)
            for item in candidates
            if isinstance(item, (MusicInfo, dict))
        ]
        return [info for info in infos if info.media_source == cls.result_source][:limit]
