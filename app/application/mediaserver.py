import re
from collections.abc import Iterable, Mapping
from typing import Any, Optional

from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.domain.context import MusicInfo
from app.schemas.media import normalize_media_source, resolve_media_identity
from app.runtime.extensions.service_registry import ServiceBaseHelper
from app.schemas.system import MediaServerConf
from app.schemas.system import ServiceInfo
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MediaSource,
    ModuleType,
    SystemConfigKey,
)


class MediaServerIdentityHelper:
    """将媒体服务器专有 ProviderIds 适配为统一媒体身份。"""

    _provider_keys = (
        (MediaSource.TMDB, ("Tmdb", "TMDB", "tmdb", "tmdb_id")),
        (MediaSource.Douban, ("Douban", "douban", "douban_id")),
        (MediaSource.Bangumi, ("Bangumi", "bangumi", "bangumi_id")),
        (MediaSource.AniList, ("AniList", "Anilist", "anilist", "anilist_id")),
        (MediaSource.IMDb, ("Imdb", "IMDb", "imdb", "imdb_id")),
        (MediaSource.TVDB, ("Tvdb", "TVDB", "tvdb", "tvdb_id")),
        (MediaSource.MusicBrainz, ("MusicBrainz", "musicbrainz", "musicbrainz_id")),
    )

    @classmethod
    def from_provider_ids(
        cls,
        provider_ids: Optional[Mapping[str, Any]],
    ) -> tuple[Optional[MediaSource], Optional[str]]:
        """按固定优先级从外部 ProviderIds 选择一个规范媒体身份。"""
        if not isinstance(provider_ids, Mapping):
            return None, None
        for media_source, keys in cls._provider_keys:
            for key in keys:
                value = provider_ids.get(key)
                if value is not None and str(value).strip():
                    return media_source, str(value).strip()
        return None, None

    @staticmethod
    def are_compatible(
        left_source: Optional[MediaSource | str],
        left_id: Optional[str],
        right_source: Optional[MediaSource | str],
        right_id: Optional[str],
    ) -> bool:
        """判断两组身份是否没有可证实的同来源 ID 冲突。"""
        left_source, left_id = resolve_media_identity(
            media_source=left_source,
            media_id=left_id,
        )
        right_source, right_id = resolve_media_identity(
            media_source=right_source,
            media_id=right_id,
        )
        if not left_source or not right_source:
            return True
        if normalize_media_source(left_source) != normalize_media_source(right_source):
            return True
        return left_id == right_id

    @classmethod
    def is_compatible(
        cls,
        item: _SchemaMediaServerItem,
        media_source: Optional[MediaSource | str],
        media_id: Optional[str],
    ) -> bool:
        """判断目标与媒体库条目是否无明确身份冲突。"""
        item_source, item_id = resolve_media_identity(media=item)
        return cls.are_compatible(item_source, item_id, media_source, media_id)


class MusicMediaServerHelper:
    """统一音乐媒体库条目的字段转换、精确匹配和整专完整性判断。"""

    _name_pattern = re.compile(r"[\W_]+", re.UNICODE)

    @classmethod
    def normalize_name(cls, value: Optional[str]) -> str:
        """忽略大小写、空白和标点，生成用于音乐名称精确比较的稳定文本。"""
        return cls._name_pattern.sub("", str(value or "").casefold())

    @classmethod
    def same_name(cls, left: Optional[str], right: Optional[str]) -> bool:
        """判断两个非空音乐名称在规范化后是否完全一致。"""
        normalized_left = cls.normalize_name(left)
        normalized_right = cls.normalize_name(right)
        return bool(normalized_left) and normalized_left == normalized_right

    @staticmethod
    def _first_value(data: Mapping[str, Any], *keys: str) -> Any:
        """按候选键顺序返回第一个非空字段，兼容不同媒体服务器命名。"""
        for key in keys:
            value = data.get(key)
            if value not in (None, "", []):
                return value
        return None

    @classmethod
    def _extract_names(cls, value: Any) -> list[str]:
        """从字符串、对象列表或名称列表中提取非空名称。"""
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, Mapping):
            name = cls._first_value(value, "Name", "name", "Title", "title")
            return [str(name)] if name and str(name).strip() else []
        if not isinstance(value, Iterable) or isinstance(value, bytes):
            return []
        names: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                name = cls._first_value(item, "Name", "name", "Title", "title")
            else:
                name = item
            if name and str(name).strip():
                names.append(str(name))
        return names

    @classmethod
    def build_note(cls, item: Mapping[str, Any]) -> dict[str, Any]:
        """把 Emby 系和 NAS 搜索结果中的音乐字段转换为统一备注结构。"""
        artists = cls._extract_names(
            cls._first_value(item, "Artists", "artists", "ArtistItems", "artist_items")
        )
        album_artists = cls._extract_names(
            cls._first_value(item, "AlbumArtists", "album_artists")
        )
        artist = cls._first_value(
            item,
            "AlbumArtist",
            "album_artist",
            "Artist",
            "artist",
            "artist_name",
            "singer",
        )
        explicit_artists = cls._extract_names(artist)
        if explicit_artists:
            artist = explicit_artists[0]
        if not artist:
            artist = next(iter(album_artists or artists), None)

        item_type = cls.normalize_name(
            cls._first_value(item, "Type", "type", "item_type")
        )
        album = cls._first_value(item, "Album", "album", "album_name")
        if not album and item_type in {"musicalbum", "album"}:
            album = cls._first_value(item, "Name", "name", "Title", "title")

        song_count = cls._first_value(
            item,
            "ChildCount",
            "child_count",
            "SongCount",
            "songCount",
            "song_count",
            "TrackCount",
            "trackCount",
            "track_count",
            "LeafCount",
            "leafCount",
        )
        return {
            "artist": str(artist) if artist is not None else None,
            "artists": artists or album_artists,
            "album": str(album) if album is not None else None,
            "song_count": song_count,
        }

    @staticmethod
    def search_params(mediainfo: MusicInfo) -> dict[str, Optional[str]]:
        """按单曲或专辑实体构造媒体服务器音乐搜索参数。"""
        is_album = getattr(mediainfo, "music_type", None) == MUSIC_ENTITY_ALBUM
        artists = getattr(mediainfo, "artists", None) or []
        artist = (
            getattr(mediainfo, "album_artist", None)
            or next(iter(artists), None)
            or getattr(mediainfo, "artist", None)
        )
        title = getattr(mediainfo, "title", None)
        album = getattr(mediainfo, "album", None) or title
        return {
            "title": None if is_album else title,
            "artist": artist,
            "album": album if is_album else None,
        }

    @classmethod
    def item_matches(
        cls,
        mediainfo: MusicInfo,
        item: _SchemaMediaServerItem,
    ) -> bool:
        """校验媒体库条目是否精确对应单曲，或完整覆盖目标专辑。"""
        note = item.note if isinstance(item.note, Mapping) else {}
        is_album = getattr(mediainfo, "music_type", None) == MUSIC_ENTITY_ALBUM
        target_title = getattr(mediainfo, "title", None)
        actual_title = item.title
        if is_album:
            target_title = getattr(mediainfo, "album", None) or target_title
            actual_title = note.get("album") or actual_title
        if not cls.same_name(actual_title, target_title):
            return False

        target_artists = [
            getattr(mediainfo, "artist", None),
            getattr(mediainfo, "album_artist", None),
            *(getattr(mediainfo, "artists", None) or []),
        ]
        target_artists = [artist for artist in target_artists if artist]
        actual_artists = [note.get("artist"), *cls._extract_names(note.get("artists"))]
        actual_artists = [artist for artist in actual_artists if artist]
        if target_artists and not any(
            cls.same_name(actual, target)
            for actual in actual_artists
            for target in target_artists
        ):
            return False

        if not is_album:
            return True
        try:
            expected_tracks = int(getattr(mediainfo, "total_tracks", None) or 0)
            actual_tracks = int(note.get("song_count") or 0)
        except (TypeError, ValueError):
            return False
        return expected_tracks > 0 and actual_tracks >= expected_tracks

    @classmethod
    def find_match(
        cls,
        mediainfo: MusicInfo,
        items: Optional[Iterable[_SchemaMediaServerItem]],
    ) -> Optional[_SchemaMediaServerItem]:
        """返回首个满足单曲精确匹配或整专完整性要求的媒体库条目。"""
        return next(
            (item for item in items or [] if item and cls.item_matches(mediainfo, item)),
            None,
        )


class MediaServerHelper(ServiceBaseHelper[MediaServerConf]):
    """管理媒体服务器配置，并按类型发现已启用的服务实例。"""

    def __init__(self) -> None:
        """绑定媒体服务器配置键、配置模型和模块类型。"""
        super().__init__(
            config_key=SystemConfigKey.MediaServers,
            conf_type=MediaServerConf,
            module_type=ModuleType.MediaServer,
        )

    def is_media_server(
        self,
        service_type: Optional[str] = None,
        service: Optional[ServiceInfo] = None,
        name: Optional[str] = None,
    ) -> bool:
        """判断给定服务或服务名称是否属于指定媒体服务器类型。"""
        service = service or self.get_service(name=name)
        return bool(service and service.type == service_type)
