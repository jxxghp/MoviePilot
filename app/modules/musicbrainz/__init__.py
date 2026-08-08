import threading
import time
from typing import Any, Optional, Tuple, Union

from app.core.cache import cached
from app.core.config import settings
from app.core.music import (
    MUSIC_ENTITY_ALBUM,
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
    MusicMeta,
    MusicRelease,
)
from app.log import logger
from app.modules import _ModuleBase
from app.schemas.types import MediaRecognizeType, ModuleType
from app.utils.http import RequestUtils


class MusicBrainzModule(_ModuleBase):
    """通过 MusicBrainz 提供音乐元数据搜索和详情识别。"""

    _source = "musicbrainz"
    _base_url = "https://musicbrainz.org/ws/2"
    _detail_url = "https://musicbrainz.org/recording"
    _album_detail_url = "https://musicbrainz.org/release-group"
    _artist_detail_url = "https://musicbrainz.org/artist"
    _cover_url = "https://coverartarchive.org/release-group"
    _request_interval = 1.0
    _request_lock = threading.Lock()
    _last_request_at = 0.0
    # 关联艺术家按关系可读性排序，纪念性质的致敬关系数量庞大且价值低，放到最后
    _artist_relation_priority = (
        "member of band",
        "subgroup",
        "collaboration",
        "founder",
        "artist rename",
        "supporting musician",
        "conductor position",
        "involved with",
        "teacher",
        "sibling",
        "parent",
        "married",
    )
    # 艺术家外链只保留对用户有意义的官方与流媒体入口
    _artist_link_types = (
        "official homepage",
        "wikidata",
        "wikipedia",
        "discogs",
        "allmusic",
        "social network",
        "free streaming",
        "streaming",
        "youtube",
        "purchase for download",
    )

    def init_module(self) -> None:
        """初始化无状态的 MusicBrainz 模块。"""

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """MusicBrainz 无需独立密钥或启用开关。"""
        return None

    def stop(self) -> None:
        """停止模块；当前实现没有需要释放的持久资源。"""

    def test(self) -> Tuple[bool, str]:
        """测试 MusicBrainz 搜索接口连通性。"""
        result = self._request_json(
            "/recording",
            params={"query": "recording:test", "limit": 1, "fmt": "json"},
        )
        return (True, "") if result is not None else (False, "MusicBrainz 网络连接失败")

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "MusicBrainz"

    @staticmethod
    def get_type() -> ModuleType:
        """返回模块所属的媒体识别类型。"""
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """返回 MusicBrainz 模块子类型。"""
        return MediaRecognizeType.MusicBrainz

    @staticmethod
    def get_priority() -> int:
        """返回音乐元数据模块执行优先级。"""
        return 5

    def search_music(self, meta: MusicMeta, limit: int = 20) -> list[MusicInfo]:
        """根据标准音乐搜索条件返回 MusicBrainz 录音候选。"""
        query = self._build_query(meta)
        if not query:
            return []
        payload = self._request_json(
            "/recording",
            params={"query": query, "limit": max(1, min(limit, 100)), "fmt": "json"},
        )
        return [
            info
            for item in (payload or {}).get("recordings") or []
            if (info := self._recording_to_info(item))
        ]

    def recognize_music(self, source: str, media_id: str) -> Optional[MusicInfo]:
        """按 MusicBrainz 标准 ID 获取音乐详情，单曲不存在时回退到专辑。"""
        if source != self._source or not media_id:
            return None
        payload = self._request_json(
            f"/recording/{media_id}",
            params={
                "inc": "artists+releases+release-groups+isrcs+genres",
                "fmt": "json",
            },
        )
        if payload:
            return self._recording_to_info(payload)
        # 订阅只持久化来源和 ID，无法区分单曲与专辑，因此按专辑再查一次
        album = self.music_album(source, media_id)
        return album.to_music_info() if album else None

    def music_album(self, source: str, media_id: str) -> Optional[MusicAlbumInfo]:
        """按 MusicBrainz Release Group ID 获取标准化专辑详情及曲目。"""
        if source != self._source or not media_id:
            return None
        payload = self._request_json(
            f"/release-group/{media_id}",
            params={
                "inc": "artists+releases+media+genres+tags+ratings",
                "fmt": "json",
            },
        )
        if not payload:
            return None
        album = self._release_group_to_album(payload)
        if not album:
            return None
        album.releases = self._release_variants(payload.get("releases") or [])
        album.tracks = self._album_tracks(album, payload.get("releases") or [])
        return album

    def music_artist(self, source: str, media_id: str) -> Optional[MusicArtistInfo]:
        """按 MusicBrainz Artist ID 获取标准化艺术家详情。"""
        if source != self._source or not media_id:
            return None
        payload = self._request_json(
            f"/artist/{media_id}",
            params={"inc": "url-rels+genres+tags+aliases", "fmt": "json"},
        )
        return self._artist_to_info(payload) if payload else None

    def music_artist_albums(
            self,
            source: str,
            media_id: str,
            page: int = 1,
            count: int = 30,
            album_type: Optional[str] = None,
    ) -> list[MusicInfo]:
        """按 MusicBrainz Artist ID 分页浏览该艺术家的专辑、EP 和单曲。"""
        if source != self._source or not media_id:
            return []
        limit = max(1, min(count, 100))
        params: dict[str, Any] = {
            "artist": media_id,
            "inc": "artist-credits",
            "limit": limit,
            "offset": max(page - 1, 0) * limit,
            "fmt": "json",
        }
        if album_type:
            params["type"] = album_type
        payload = self._request_json("/release-group", params=params)
        albums = [
            album
            for item in (payload or {}).get("release-groups") or []
            if (album := self._release_group_to_album(item))
        ]
        # MusicBrainz 浏览接口不支持排序，只能在当前页内按发行日期倒序，保证首页是最新作品
        albums.sort(key=lambda item: item.release_date or "", reverse=True)
        return [album.to_music_info() for album in albums]

    def music_artist_related(
            self,
            source: str,
            media_id: str,
            count: int = 24,
    ) -> list[MusicArtistInfo]:
        """按 MusicBrainz 艺术家关系返回可继续浏览的关联艺术家。"""
        if source != self._source or not media_id:
            return []
        payload = self._request_json(
            f"/artist/{media_id}",
            params={"inc": "artist-rels", "fmt": "json"},
        )
        if not payload:
            return []
        return self._related_artists(payload.get("relations") or [], count=count)

    @classmethod
    def _build_query(cls, meta: MusicMeta) -> str:
        """构造 MusicBrainz Recording 搜索表达式。"""
        clauses = []
        if meta.title:
            clauses.append(f'recording:"{cls._escape_query(meta.title)}"')
        if meta.artists:
            clauses.append(f'artist:"{cls._escape_query(meta.artists[0])}"')
        if meta.album:
            clauses.append(f'release:"{cls._escape_query(meta.album)}"')
        if meta.isrc:
            clauses.append(f'isrc:"{cls._escape_query(meta.isrc)}"')
        return " AND ".join(clauses)

    @staticmethod
    def _escape_query(value: str) -> str:
        """转义 MusicBrainz 查询中的引号和反斜线。"""
        return value.replace("\\", "\\\\").replace('"', '\\"').strip()

    @classmethod
    def _recording_to_info(cls, recording: dict[str, Any]) -> Optional[MusicInfo]:
        """将 MusicBrainz Recording 响应转换为标准音乐信息。"""
        media_id = recording.get("id")
        title = recording.get("title")
        if not media_id or not title:
            return None
        releases = recording.get("releases") or []
        release = cls._select_release(releases)
        release_group = (release or {}).get("release-group") or {}
        release_date = cls._release_date(recording, release)
        album = (release or {}).get("title")
        artists, artist_ids = cls._artist_credits(recording.get("artist-credit"))
        album_artists, _ = cls._artist_credits((release or {}).get("artist-credit"))
        category_parts = [release_group.get("primary-type")]
        category_parts.extend(release_group.get("secondary-types") or [])
        return MusicInfo(
            source=cls._source,
            media_id=str(media_id),
            title=str(title),
            artists=artists,
            artist_ids=artist_ids,
            album=album,
            album_artist=" / ".join(album_artists) if album_artists else None,
            album_id=str(release_group["id"]) if release_group.get("id") else None,
            album_type=release_group.get("primary-type"),
            year=cls._year(release_date),
            release_date=release_date,
            duration=cls._duration_seconds(recording.get("length")),
            isrc=next(iter(recording.get("isrcs") or []), None),
            cover_url=cls._build_cover_url(release_group.get("id")),
            version=recording.get("disambiguation") or None,
            category=" / ".join(str(part) for part in category_parts if part),
            genres=cls._names_of(recording.get("genres")),
            names=[name for name in (title, album) if name],
            detail_link=f"{cls._detail_url}/{media_id}",
            raw_data=recording,
        )

    @classmethod
    def _release_group_to_album(cls, release_group: dict[str, Any]) -> Optional[MusicAlbumInfo]:
        """将 MusicBrainz Release Group 响应转换为标准专辑信息。"""
        media_id = release_group.get("id")
        title = release_group.get("title")
        if not media_id or not title:
            return None
        artists, artist_ids = cls._artist_credits(release_group.get("artist-credit"))
        rating = release_group.get("rating") or {}
        return MusicAlbumInfo(
            source=cls._source,
            media_id=str(media_id),
            title=str(title),
            artists=artists,
            artist_ids=artist_ids,
            album_type=release_group.get("primary-type"),
            secondary_types=[str(item) for item in release_group.get("secondary-types") or []],
            release_date=release_group.get("first-release-date") or None,
            cover_url=cls._build_cover_url(media_id),
            genres=cls._names_of(release_group.get("genres")),
            tags=cls._names_of(release_group.get("tags")),
            # MusicBrainz 评分是 5 分制，统一放大到与影视一致的 10 分制展示
            rating=round(float(rating["value"]) * 2, 1) if rating.get("value") else 0.0,
            rating_votes=rating.get("votes-count"),
            detail_link=f"{cls._album_detail_url}/{media_id}",
            raw_data=release_group,
        )

    @classmethod
    def _release_variants(cls, releases: list[dict[str, Any]]) -> list[MusicRelease]:
        """整理同一专辑下的发行版本，供详情页对比介质和地区。"""
        variants = []
        for release in releases:
            if not release.get("id"):
                continue
            media = release.get("media") or []
            variants.append(
                MusicRelease(
                    media_id=str(release["id"]),
                    title=release.get("title"),
                    date=release.get("date") or None,
                    country=release.get("country") or None,
                    status=release.get("status") or None,
                    packaging=release.get("packaging") or None,
                    formats=[str(item["format"]) for item in media if item.get("format")],
                    track_count=sum(int(item.get("track-count") or 0) for item in media) or None,
                )
            )
        variants.sort(key=lambda item: (cls._date_sort_key(item.date), item.title or ""))
        return variants

    @staticmethod
    def _artist_credits(
            artist_credit: Optional[list[dict[str, Any]]],
    ) -> Tuple[list[str], list[str]]:
        """从 MusicBrainz artist-credit 提取有序艺术家名称和按位置对齐的标准 ID。"""
        names: list[str] = []
        ids: list[str] = []
        for credit in artist_credit or []:
            artist = credit.get("artist") or {}
            name = artist.get("name") or credit.get("name")
            if not name or str(name) in names:
                continue
            names.append(str(name))
            # 名称与 ID 按下标一一对应，缺少 ID 时补空串，前端据此决定是否可跳转
            ids.append(str(artist.get("id") or ""))
        return names, ids

    @staticmethod
    def _names_of(items: Optional[list[dict[str, Any]]]) -> list[str]:
        """提取 MusicBrainz 风格、标签或别名列表的名称，热度高的排在前面。"""
        entries = [item for item in items or [] if item.get("name")]
        entries.sort(key=lambda item: (-int(item.get("count") or 0), str(item["name"])))
        return [str(item["name"]) for item in entries]

    @classmethod
    def _select_track_release(cls, releases: list[dict[str, Any]]) -> dict[str, Any]:
        """选择曲目最完整且发行最早的正式版本，作为专辑曲目来源。"""
        candidates = [
            release
            for release in releases
            if release.get("id")
            and sum(int(item.get("track-count") or 0) for item in release.get("media") or [])
        ]
        if not candidates:
            return next((release for release in releases if release.get("id")), {})
        official = [release for release in candidates if release.get("status") == "Official"]
        return min(
            official or candidates,
            key=lambda release: cls._date_sort_key(release.get("date")),
        )

    @classmethod
    def _album_tracks(
            cls,
            album: MusicAlbumInfo,
            releases: list[dict[str, Any]],
    ) -> list[MusicInfo]:
        """读取专辑代表性发行版本的曲目，作为专辑内音乐列表。"""
        release = cls._select_track_release(releases)
        if not release.get("id"):
            return []
        payload = cls._request_json(
            f"/release/{release['id']}",
            params={"inc": "recordings+artist-credits", "fmt": "json"},
        )
        tracks: list[MusicInfo] = []
        for medium in (payload or {}).get("media") or []:
            for track in medium.get("tracks") or []:
                info = cls._track_to_info(album, medium, track)
                if info:
                    tracks.append(info)
        return tracks

    @classmethod
    def _track_to_info(
            cls,
            album: MusicAlbumInfo,
            medium: dict[str, Any],
            track: dict[str, Any],
    ) -> Optional[MusicInfo]:
        """将发行版本中的单条曲目转换为可继续浏览的音乐信息。"""
        recording = track.get("recording") or {}
        media_id = recording.get("id")
        title = track.get("title") or recording.get("title")
        if not media_id or not title:
            return None
        artists, artist_ids = cls._artist_credits(
            track.get("artist-credit") or recording.get("artist-credit")
        )
        return MusicInfo(
            source=cls._source,
            media_id=str(media_id),
            title=str(title),
            artists=artists or list(album.artists),
            artist_ids=artist_ids or list(album.artist_ids),
            album=album.title,
            album_artist=album.artist or None,
            album_id=album.media_id,
            album_type=album.album_type,
            year=album.year,
            release_date=recording.get("first-release-date") or album.release_date,
            disc_number=cls._optional_int(medium.get("position")),
            track_number=cls._optional_int(track.get("position")),
            total_tracks=cls._optional_int(medium.get("track-count")),
            duration=cls._duration_seconds(track.get("length") or recording.get("length")),
            cover_url=album.cover_url,
            version=recording.get("disambiguation") or None,
            category=album.category,
            names=[str(title)],
            detail_link=f"{cls._detail_url}/{media_id}",
        )

    @classmethod
    def _select_release(cls, releases: list[dict[str, Any]]) -> dict[str, Any]:
        """优先选择正式且日期最早的发行记录。"""
        if not releases:
            return {}
        official = [release for release in releases if release.get("status") == "Official"]
        candidates = official or releases
        return min(
            candidates,
            key=lambda release: cls._date_sort_key(release.get("date")),
        )

    @staticmethod
    def _date_sort_key(value: Optional[str]) -> tuple[int, str]:
        """将完整或不完整发行日期转换为稳定排序键。"""
        return (0, value) if value else (1, "")

    @staticmethod
    def _release_date(recording: dict[str, Any], release: dict[str, Any]) -> Optional[str]:
        """从录音和发行信息中选择最可靠的发行日期。"""
        return recording.get("first-release-date") or release.get("date")

    @staticmethod
    def _year(release_date: Optional[str]) -> Optional[int]:
        """从 MusicBrainz 的可变精度日期提取年份。"""
        if not release_date:
            return None
        try:
            return int(release_date[:4])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _duration_seconds(value: Any) -> Optional[int]:
        """将 MusicBrainz 毫秒时长转换为整数秒。"""
        try:
            return round(int(value) / 1000) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        """将 MusicBrainz 的碟号、音轨号等计数字段转换为可选整数。"""
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _artist_to_info(
            cls,
            artist: dict[str, Any],
            relation: Optional[str] = None,
            include_raw: bool = False,
    ) -> Optional[MusicArtistInfo]:
        """将 MusicBrainz Artist 响应转换为标准艺术家信息。"""
        media_id = artist.get("id")
        name = artist.get("name")
        if not media_id or not name:
            return None
        life_span = artist.get("life-span") or {}
        area = artist.get("area") or {}
        begin_area = artist.get("begin-area") or {}
        relations = artist.get("relations") or []
        return MusicArtistInfo(
            source=cls._source,
            media_id=str(media_id),
            name=str(name),
            sort_name=artist.get("sort-name") or None,
            disambiguation=artist.get("disambiguation") or None,
            artist_type=artist.get("type") or None,
            gender=artist.get("gender") or None,
            country=artist.get("country") or None,
            area=area.get("name") or begin_area.get("name") or None,
            begin_date=life_span.get("begin") or None,
            end_date=life_span.get("end") or None,
            ended=bool(life_span.get("ended")),
            genres=cls._names_of(artist.get("genres")),
            tags=cls._names_of(artist.get("tags")),
            aliases=cls._names_of(artist.get("aliases")),
            relation=relation,
            image_url=cls._artist_image(relations),
            detail_link=f"{cls._artist_detail_url}/{media_id}",
            external_links=cls._artist_links(relations),
            raw_data=artist if include_raw else {},
        )

    @classmethod
    def _artist_image(cls, relations: list[dict[str, Any]]) -> Optional[str]:
        """从艺术家 image 关系解析可直接展示的图片地址。"""
        for relation in relations:
            if relation.get("type") != "image":
                continue
            resource = (relation.get("url") or {}).get("resource") or ""
            # MusicBrainz 记录的是维基共享资源页地址，需要转成文件直链才能展示
            if "commons.wikimedia.org/wiki/File:" in resource:
                file_name = resource.rsplit("File:", 1)[-1]
                return (
                    "https://commons.wikimedia.org/wiki/Special:FilePath/"
                    f"{file_name}?width=500"
                )
            if resource:
                return resource
        return None

    @classmethod
    def _artist_links(cls, relations: list[dict[str, Any]]) -> dict[str, str]:
        """整理艺术家可对外跳转的官方与流媒体链接。"""
        links: dict[str, str] = {}
        for relation in relations:
            relation_type = str(relation.get("type") or "")
            resource = (relation.get("url") or {}).get("resource")
            if relation_type in cls._artist_link_types and resource:
                links.setdefault(relation_type, str(resource))
        return links

    @classmethod
    def _related_artists(
            cls,
            relations: list[dict[str, Any]],
            count: int,
    ) -> list[MusicArtistInfo]:
        """按关系类型优先级整理关联艺术家，并按来源去重。"""
        ranked: list[tuple[int, MusicArtistInfo]] = []
        seen: set[str] = set()
        fallback_priority = len(cls._artist_relation_priority)
        for relation in relations:
            if relation.get("target-type") != "artist":
                continue
            artist = relation.get("artist") or {}
            artist_id = str(artist.get("id") or "")
            if not artist_id or artist_id in seen:
                continue
            relation_type = str(relation.get("type") or "")
            info = cls._artist_to_info(artist, relation=relation_type or None)
            if not info:
                continue
            seen.add(artist_id)
            priority = (
                cls._artist_relation_priority.index(relation_type)
                if relation_type in cls._artist_relation_priority
                else fallback_priority
            )
            ranked.append((priority, info))
        ranked.sort(key=lambda item: (item[0], item[1].name or ""))
        return [info for _, info in ranked[: max(1, count)]]

    @classmethod
    def _build_cover_url(cls, release_group_id: Optional[str]) -> Optional[str]:
        """根据 Release Group ID 构造 Cover Art Archive 封面地址。"""
        if not release_group_id:
            return None
        return f"{cls._cover_url}/{release_group_id}/front-500"

    @classmethod
    def _wait_for_rate_limit(cls) -> None:
        """串行控制 MusicBrainz 公共接口的最小请求间隔。"""
        with cls._request_lock:
            now = time.monotonic()
            remaining = cls._request_interval - (now - cls._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
            cls._last_request_at = time.monotonic()

    @classmethod
    @cached(maxsize=settings.CONF.musicbrainz, ttl=settings.CONF.meta, skip_none=True)
    def _request_json(
            cls,
            path: str,
            params: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """请求 MusicBrainz JSON 接口并统一处理网络和响应错误。"""
        cls._wait_for_rate_limit()
        response = RequestUtils(
            headers={
                "User-Agent": f"{settings.USER_AGENT} (https://github.com/jxxghp/MoviePilot)",
                "Accept": "application/json",
            },
            proxies=settings.PROXY,
            timeout=20,
        ).get_res(f"{cls._base_url}{path}", params=params)
        if not response:
            return None
        try:
            if response.status_code == 404:
                # 单曲与专辑共用同一套 ID 入口，404 属于正常的探测结果
                logger.debug(f"MusicBrainz 资源不存在：{path}")
                return None
            if response.status_code != 200:
                logger.warning(
                    f"MusicBrainz 请求失败：{response.status_code} {response.text[:200]}"
                )
                return None
            return response.json()
        except (TypeError, ValueError) as err:
            logger.warning(f"MusicBrainz 响应解析失败：{err}")
            return None
        finally:
            response.close()
