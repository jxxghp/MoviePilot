from typing import Any, Optional, Tuple, Union

from app.core.config import settings
from app.core.music import MusicInfo
from app.log import logger
from app.modules import _ModuleBase
from app.schemas.types import ModuleType, OtherModulesType
from app.utils.http import RequestUtils


class ListenBrainzModule(_ModuleBase):
    """通过 ListenBrainz 全站统计提供音乐推荐与探索榜单。"""

    _base_url = "https://api.listenbrainz.org/1"
    _detail_url = "https://musicbrainz.org/recording"
    _cover_url = "https://coverartarchive.org/release"
    _source = "musicbrainz"

    def init_module(self) -> None:
        """初始化无状态的 ListenBrainz 榜单模块。"""

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """ListenBrainz 公共榜单无需独立密钥或启用开关。"""
        return None

    def stop(self) -> None:
        """停止模块；当前实现没有需要释放的持久资源。"""

    def test(self) -> Tuple[bool, str]:
        """测试 ListenBrainz 全站榜单接口连通性。"""
        result = self._request_chart(range_name="this_week", offset=0, count=1)
        return (True, "") if result is not None else (False, "ListenBrainz 网络连接失败")

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "ListenBrainz"

    @staticmethod
    def get_type() -> ModuleType:
        """返回模块所属的其它能力类型。"""
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """返回 ListenBrainz 模块子类型。"""
        return OtherModulesType.ListenBrainz

    @staticmethod
    def get_priority() -> int:
        """返回音乐榜单模块执行优先级。"""
        return 5

    def music_chart(
            self,
            range_name: str,
            offset: int = 0,
            count: int = 30,
    ) -> list[MusicInfo]:
        """读取指定统计周期的全站录音榜单。"""
        payload = self._request_chart(
            range_name=range_name,
            offset=max(offset, 0),
            count=max(1, min(count, 100)),
        )
        recordings = ((payload or {}).get("payload") or {}).get("recordings") or []
        return [
            info
            for item in recordings
            if (info := self._recording_to_info(item))
        ]

    @classmethod
    def _request_chart(
            cls,
            range_name: str,
            offset: int,
            count: int,
    ) -> Optional[dict[str, Any]]:
        """请求 ListenBrainz 全站录音统计并统一处理异常响应。"""
        response = RequestUtils(
            headers={
                "User-Agent": f"{settings.USER_AGENT} (https://github.com/jxxghp/MoviePilot)",
                "Accept": "application/json",
            },
            proxies=settings.PROXY,
            timeout=20,
        ).get_res(
            f"{cls._base_url}/stats/sitewide/recordings",
            params={
                "range": range_name,
                "offset": offset,
                "count": count,
            },
        )
        if not response:
            return None
        try:
            if response.status_code != 200:
                logger.warning(
                    f"ListenBrainz 请求失败：{response.status_code} {response.text[:200]}"
                )
                return None
            return response.json()
        except (TypeError, ValueError) as err:
            logger.warning(f"ListenBrainz 响应解析失败：{err}")
            return None
        finally:
            response.close()

    @classmethod
    def _recording_to_info(cls, recording: dict[str, Any]) -> Optional[MusicInfo]:
        """将 ListenBrainz 录音统计转换为标准音乐信息。"""
        media_id = recording.get("recording_mbid")
        title = recording.get("track_name")
        if not media_id or not title:
            return None
        artist_name = str(recording.get("artist_name") or "").strip()
        release_name = str(recording.get("release_name") or "").strip()
        release_mbid = recording.get("caa_release_mbid") or recording.get("release_mbid")
        return MusicInfo(
            source=cls._source,
            media_id=str(media_id),
            title=str(title),
            artists=[artist_name] if artist_name else [],
            album=release_name or None,
            cover_url=f"{cls._cover_url}/{release_mbid}/front-500" if release_mbid else None,
            names=[name for name in (title, release_name) if name],
            detail_link=f"{cls._detail_url}/{media_id}",
            listen_count=cls._optional_int(recording.get("listen_count")),
            raw_data=recording,
        )

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        """把 ListenBrainz 统计值转换为可选整数。"""
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
