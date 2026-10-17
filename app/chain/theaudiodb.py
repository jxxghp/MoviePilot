"""
绑定 TheAudioDB 来源的音乐搜索、识别与详情调用，关联艺人查询返回空列表。
"""

from app.chain.musicbrainz import MusicMetadataSourceChain
from app.domain.context import MusicArtistInfo
from app.schemas.types import MediaSource


class TheAudioDbChain(MusicMetadataSourceChain):
    """
    TheAudioDB 音乐搜索、识别与详情来源链。
    """

    source = MediaSource.TheAudioDB

    async def async_get_music_artist_related(
            self,
            media_id: str,
            count: int = 24,
    ) -> list[MusicArtistInfo]:
        """
        返回 TheAudioDB 关联艺术家；当前来源模块未提供该能力。
        """
        del media_id, count
        return []
