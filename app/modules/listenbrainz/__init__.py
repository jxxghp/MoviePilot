from typing import Any, Optional, Tuple, Union

from app.runtime.cache import cached
from app.runtime.settings import get_runtime_setting

from app.domain.context import MusicInfo
from app.runtime.log import logger
from app.modules import _ModuleBase
from app.schemas.types import (
    LISTENBRAINZ_CHART_RANGES,
    LISTENBRAINZ_FRESH_MAX_DAYS,
    LISTENBRAINZ_FRESH_SORTS,
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    ModuleType,
    OtherModulesType,
)
from app.adapters.network.http import RequestUtils


class ListenBrainzModule(_ModuleBase):
    """通过 ListenBrainz 全站统计与新发行数据提供音乐探索能力。"""

    _base_url = "https://api.listenbrainz.org/1"
    _detail_url = "https://musicbrainz.org/recording"
    _album_detail_url = "https://musicbrainz.org/release-group"
    _release_cover_url = "https://coverartarchive.org/release"
    _release_group_cover_url = "https://coverartarchive.org/release-group"
    _source = MediaSource.MusicBrainz
    # 全站统计按实体分为不同接口，键为音乐实体类型，值为接口路径与数据字段
    _chart_entities = {
        MUSIC_ENTITY_RECORDING: ("recordings", "recordings"),
        MUSIC_ENTITY_ALBUM: ("release-groups", "release_groups"),
    }

    def init_module(self) -> None:
        """初始化无状态的 ListenBrainz 探索模块。"""

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """ListenBrainz 公共数据无需独立密钥或启用开关。"""
        return None

    def stop(self) -> None:
        """停止模块；当前实现没有需要释放的持久资源。"""

    def test(self) -> Tuple[bool, str]:
        """测试 ListenBrainz 全站榜单接口连通性。"""
        result = self._request_chart(entity="recordings", range_name="this_week", offset=0, count=1)
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
            entity: str = MUSIC_ENTITY_RECORDING,
    ) -> list[MusicInfo]:
        """读取指定周期的全站热门单曲或热门专辑榜单。"""
        path, field = self._chart_entities.get(
            entity, self._chart_entities[MUSIC_ENTITY_RECORDING]
        )
        payload = self._request_chart(
            entity=path,
            range_name=range_name if range_name in LISTENBRAINZ_CHART_RANGES else "this_month",
            offset=max(offset, 0),
            count=max(1, min(count, 100)),
        )
        items = ((payload or {}).get("payload") or {}).get(field) or []
        convert = (
            self._release_group_to_info
            if entity == MUSIC_ENTITY_ALBUM
            else self._recording_to_info
        )
        return [info for item in items if (info := convert(item))]

    def music_fresh_releases(
            self,
            days: int = 14,
            sort: str = "release_date",
            past: bool = True,
            future: bool = True,
            offset: int = 0,
            count: int = 30,
    ) -> list[MusicInfo]:
        """读取 ListenBrainz 官方新发行专辑，并按官方排序方式分页返回。"""
        releases = self._fresh_releases(
            days=max(1, min(days, LISTENBRAINZ_FRESH_MAX_DAYS)),
            sort=sort if sort in LISTENBRAINZ_FRESH_SORTS else "release_date",
            past=bool(past),
            future=bool(future),
        )
        # 官方接口一次性返回整个时间窗口的全部发行，分页只能在结果集上切片
        start = max(offset, 0)
        window = releases[start: start + max(1, min(count, 100))]
        return [info for item in window if (info := self._fresh_release_to_info(item))]

    @cached(maxsize=32, ttl=1800, skip_empty=True)
    def _fresh_releases(
            self,
            days: int,
            sort: str,
            past: bool,
            future: bool,
    ) -> list[dict[str, Any]]:
        """缓存官方新发行原始结果，避免翻页时重复拉取整个时间窗口。"""
        payload = self._request_json(
            "/explore/fresh-releases/",
            params={
                "days": days,
                "sort": sort,
                "past": str(past).lower(),
                "future": str(future).lower(),
            },
        )
        return ((payload or {}).get("payload") or {}).get("releases") or []

    @classmethod
    def _request_chart(
            cls,
            entity: str,
            range_name: str,
            offset: int,
            count: int,
    ) -> Optional[dict[str, Any]]:
        """请求 ListenBrainz 全站统计接口。"""
        return cls._request_json(
            f"/stats/sitewide/{entity}",
            params={
                "range": range_name,
                "offset": offset,
                "count": count,
            },
        )

    @classmethod
    @cached(maxsize=get_runtime_setting('CONF').listenbrainz, ttl=get_runtime_setting('CONF').meta, skip_none=True)
    def _request_json(
            cls,
            path: str,
            params: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """请求 ListenBrainz JSON 接口并统一处理网络和响应错误。"""
        response = RequestUtils(
            headers={
                "User-Agent": f"{get_runtime_setting('USER_AGENT')} (https://github.com/jxxghp/MoviePilot)",
                "Accept": "application/json",
            },
            proxies=get_runtime_setting('PROXY'),
            timeout=20,
        ).get_res(f"{cls._base_url}{path}", params=params)
        if response is None:
            return None
        try:
            if response.status_code == 204:
                # 统计尚未生成时官方返回 204，视为空结果而不是失败
                logger.debug(f"ListenBrainz 统计尚未生成：{path}")
                return {}
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
        """将 ListenBrainz 热门单曲统计转换为标准音乐信息。"""
        media_id = recording.get("recording_mbid")
        title = recording.get("track_name")
        if not media_id or not title:
            return None
        artist_name = str(recording.get("artist_name") or "").strip()
        release_name = str(recording.get("release_name") or "").strip()
        release_mbid = recording.get("caa_release_mbid") or recording.get("release_mbid")
        return MusicInfo(
            media_source=cls._source,
            media_id=str(media_id),
            music_type=MUSIC_ENTITY_RECORDING,
            title=str(title),
            artists=[artist_name] if artist_name else [],
            artist_ids=cls._primary_artist_ids(recording.get("artist_mbids")),
            album=release_name or None,
            cover_url=cls._release_cover(release_mbid),
            names=[name for name in (title, release_name) if name],
            detail_link=f"{cls._detail_url}/{media_id}",
            listen_count=cls._optional_int(recording.get("listen_count")),
            raw_data=recording,
        )

    @classmethod
    def _release_group_to_info(cls, release_group: dict[str, Any]) -> Optional[MusicInfo]:
        """将 ListenBrainz 热门专辑统计转换为标准音乐信息。"""
        media_id = release_group.get("release_group_mbid")
        title = release_group.get("release_group_name")
        if not media_id or not title:
            return None
        artist_name = str(release_group.get("artist_name") or "").strip()
        return MusicInfo(
            media_source=cls._source,
            media_id=str(media_id),
            music_type=MUSIC_ENTITY_ALBUM,
            title=str(title),
            artists=[artist_name] if artist_name else [],
            artist_ids=cls._primary_artist_ids(release_group.get("artist_mbids")),
            album=str(title),
            album_artist=artist_name or None,
            album_id=str(media_id),
            cover_url=cls._release_cover(release_group.get("caa_release_mbid"))
            or cls._release_group_cover(media_id),
            names=[str(title)],
            detail_link=f"{cls._album_detail_url}/{media_id}",
            listen_count=cls._optional_int(release_group.get("listen_count")),
            raw_data=release_group,
        )

    @classmethod
    def _fresh_release_to_info(cls, release: dict[str, Any]) -> Optional[MusicInfo]:
        """将 ListenBrainz 新发行条目转换为标准专辑信息。"""
        media_id = release.get("release_group_mbid")
        title = release.get("release_name")
        if not media_id or not title:
            return None
        artist_name = str(release.get("artist_credit_name") or "").strip()
        release_date = release.get("release_date") or None
        primary_type = cls._stripped(release.get("release_group_primary_type"))
        secondary_type = cls._stripped(release.get("release_group_secondary_type"))
        category_parts = [primary_type, secondary_type]
        return MusicInfo(
            media_source=cls._source,
            media_id=str(media_id),
            music_type=MUSIC_ENTITY_ALBUM,
            title=str(title),
            artists=[artist_name] if artist_name else [],
            artist_ids=cls._primary_artist_ids(release.get("artist_mbids")),
            album=str(title),
            album_artist=artist_name or None,
            album_id=str(media_id),
            album_type=primary_type,
            secondary_types=[secondary_type] if secondary_type else [],
            year=cls._year(release_date),
            release_date=release_date,
            cover_url=cls._release_cover(release.get("caa_release_mbid"))
            or cls._release_group_cover(media_id),
            metadata_category=" / ".join(part for part in category_parts if part),
            genres=[str(tag) for tag in release.get("release_tags") or [] if tag],
            names=[str(title)],
            detail_link=f"{cls._album_detail_url}/{media_id}",
            listen_count=cls._optional_int(release.get("listen_count")),
            raw_data=release,
        )

    @classmethod
    def _release_cover(cls, release_mbid: Any) -> Optional[str]:
        """根据发行版本 ID 构造 Cover Art Archive 封面地址。"""
        if not release_mbid:
            return None
        # 支持配置音乐封面代理地址，解决 coverartarchive.org 无法访问的问题
        base = (get_runtime_setting('MUSIC_COVER_PROXY') or "https://coverartarchive.org").rstrip("/")
        return f"{base}/release/{release_mbid}/front-500"

    @classmethod
    def _release_group_cover(cls, release_group_id: Any) -> Optional[str]:
        """根据 Release Group ID 构造 Cover Art Archive 封面地址。"""
        if not release_group_id:
            return None
        # 支持配置音乐封面代理地址，解决 coverartarchive.org 无法访问的问题
        base = (get_runtime_setting('MUSIC_COVER_PROXY') or "https://coverartarchive.org").rstrip("/")
        return f"{base}/release-group/{release_group_id}/front-500"

    @staticmethod
    def _primary_artist_ids(artist_mbids: Any) -> list[str]:
        """返回可跳转的主艺术家 ID。

        ListenBrainz 的艺术家名称是合并署名文本，无法与多个 ID 一一对应，
        因此只保留首个 ID 作为卡片上的主艺术家入口。
        """
        if not isinstance(artist_mbids, (list, tuple)):
            return []
        return [str(artist_mbids[0])] if artist_mbids and artist_mbids[0] else []

    @staticmethod
    def _stripped(value: Any) -> Optional[str]:
        """去除 ListenBrainz 响应字段两端的空白，空值返回 None。"""
        text = str(value).strip() if value is not None else ""
        return text or None

    @staticmethod
    def _year(release_date: Any) -> Optional[int]:
        """从 ListenBrainz 发行日期提取年份。"""
        text = str(release_date or "")[:4]
        return int(text) if text.isdigit() else None

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        """把 ListenBrainz 统计值转换为可选整数。"""
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
