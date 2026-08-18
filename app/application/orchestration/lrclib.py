from typing import Optional, Union

from app.application.orchestration import ChainBase
from app.domain.context import MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MetaMusic


class LrclibChain(ChainBase):
    """LRCLIB 音乐歌词来源链。"""

    def get_music_lyrics(
            self,
            music: Union[MetaMusic, MusicInfo],
    ) -> Optional[MusicLyrics]:
        """按单曲元数据获取标准化歌词。"""
        result = self.unicast("music_lyrics", music=music)
        if isinstance(result, MusicLyrics):
            return result
        return MusicLyrics.from_dict(result) if isinstance(result, dict) else None

    async def async_get_music_lyrics(
            self,
            music: Union[MetaMusic, MusicInfo],
    ) -> Optional[MusicLyrics]:
        """异步按单曲元数据获取标准化歌词。"""
        result = await self.async_unicast("music_lyrics", music=music)
        if isinstance(result, MusicLyrics):
            return result
        return MusicLyrics.from_dict(result) if isinstance(result, dict) else None
