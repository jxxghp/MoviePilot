from pathlib import Path
from typing import Optional, Union

from app.chain import ChainBase


class AcoustIdChain(ChainBase):
    """AcoustID 音频指纹识别来源链。"""

    def identify_music_by_fingerprint(
            self,
            path: Union[str, Path],
    ) -> Optional[str]:
        """根据本地音频指纹返回 MusicBrainz Recording ID。"""
        result = self.unicast(
            "identify_music_by_fingerprint",
            path=Path(path),
        )
        return str(result).strip() if result else None

    async def async_identify_music_by_fingerprint(
            self,
            path: Union[str, Path],
    ) -> Optional[str]:
        """异步根据本地音频指纹返回 MusicBrainz Recording ID。"""
        result = await self.async_unicast(
            "async_identify_music_by_fingerprint",
            path=Path(path),
        )
        return str(result).strip() if result else None
