"""媒体识别、音乐、搜索与来源投影的稳定 Facade。"""

from typing import Any, Awaitable, Callable, Optional, Union, cast

from app.chain.base import ChainBase
from app.chain.media.album import MediaAlbumOwner
from app.chain.media.auxiliary import MediaAuxiliaryOwner
from app.chain.media.cache import AlbumDirectoryCache
from app.chain.media.catalog import MediaCatalogOwner
from app.chain.media.path import MediaPathOwner
from app.chain.media.plugin import MediaPluginOwner
from app.chain.media.projection import MediaProjectionOwner
from app.chain.media.recognition import MediaRecognitionOwner
from app.chain.media.search import MediaSearchOwner
from app.domain.context import Context, MediaInfo, MusicAlbumInfo, MusicArtistInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.foundation.singleton import Singleton
from app.schemas.types import MediaSource

_PUBLIC_MODULE = "app.chain.media"
_MusicListCall = Callable[..., list[MusicInfo]]
_AsyncMusicListCall = Callable[..., Awaitable[list[MusicInfo]]]
_MusicCall = Callable[..., Optional[MusicInfo]]
_AsyncMusicCall = Callable[..., Awaitable[Optional[MusicInfo]]]
_MediaCall = Callable[..., Optional[MediaInfo]]
_AsyncMediaCall = Callable[..., Awaitable[Optional[MediaInfo]]]
_MediaOrMusicCall = Callable[..., Optional[Union[MediaInfo, MusicInfo]]]
_AsyncMediaOrMusicCall = Callable[
    ..., Awaitable[Optional[Union[MediaInfo, MusicInfo]]]
]
_AlbumMappingCall = Callable[..., dict[str, MusicInfo]]
_AsyncAlbumMappingCall = Callable[..., Awaitable[dict[str, MusicInfo]]]
_ContextCall = Callable[..., Optional[Context]]
_AsyncContextCall = Callable[..., Awaitable[Optional[Context]]]
_ProjectionCall = Callable[..., Optional[dict[str, Any]]]
_AsyncProjectionCall = Callable[..., Awaitable[Optional[dict[str, Any]]]]
_SearchCall = Callable[..., tuple[Optional[MetaBase], list[MediaInfo]]]
_AsyncSearchCall = Callable[
    ..., Awaitable[tuple[Optional[MetaBase], list[MediaInfo]]]
]


class MediaChain(ChainBase, metaclass=Singleton):
    """组合识别、音乐目录、搜索和来源投影的稳定媒体处理门面。"""

    __module__ = _PUBLIC_MODULE

    _video_primary_source = MediaSource.TMDB
    _music_primary_source = MediaSource.MusicBrainz
    _album_dir_cache_max = 128
    _album_dir_cache = AlbumDirectoryCache(_album_dir_cache_max)
    _album_match_min_files = 2
    _music_simplified_text_fields = (
        "title",
        "album",
        "album_artist",
        "album_type",
        "version",
        "metadata_category",
    )
    _music_simplified_list_fields = (
        "artists",
        "secondary_types",
        "genres",
        "tags",
        "names",
    )

    _music_source_chain = staticmethod(MediaCatalogOwner._music_source_chain)
    _music_search_sources = classmethod(  # type: ignore[var-annotated]
        MediaCatalogOwner._music_search_sources.__func__  # type: ignore[attr-defined]
    )
    _async_search_music_source = staticmethod(MediaCatalogOwner._async_search_music_source)
    normalize_music_candidates = classmethod(  # type: ignore[var-annotated]
        MediaCatalogOwner.normalize_music_candidates.__func__  # type: ignore[attr-defined]
    )
    _music_catalog = MediaCatalogOwner._music_catalog
    search_music = cast(_MusicListCall, MediaCatalogOwner.search_music)
    async_search_music = cast(_AsyncMusicListCall, MediaCatalogOwner.async_search_music)
    _validate_music_result = classmethod(  # type: ignore[var-annotated]
        MediaCatalogOwner._validate_music_result.__func__  # type: ignore[attr-defined]
    )
    _simplify_recognized_music_info = classmethod(  # type: ignore[var-annotated]
        MediaCatalogOwner._simplify_recognized_music_info.__func__  # type: ignore[attr-defined]
    )
    _simplify_recognized_music_mapping = classmethod(  # type: ignore[var-annotated]
        MediaCatalogOwner._simplify_recognized_music_mapping.__func__  # type: ignore[attr-defined]
    )
    recognize_music_from_source = cast(
        _MusicCall, MediaCatalogOwner.recognize_music_from_source
    )
    async_recognize_music_from_source = cast(
        _AsyncMusicCall, MediaCatalogOwner.async_recognize_music_from_source
    )
    get_music_album = cast(
        Callable[..., Optional[MusicAlbumInfo]], MediaCatalogOwner.get_music_album
    )
    async_get_music_album = cast(
        Callable[..., Awaitable[Optional[MusicAlbumInfo]]],
        MediaCatalogOwner.async_get_music_album,
    )
    async_get_music_album_related = cast(
        _AsyncMusicListCall, MediaCatalogOwner.async_get_music_album_related
    )
    async_get_music_artist = cast(
        Callable[..., Awaitable[Optional[MusicArtistInfo]]],
        MediaCatalogOwner.async_get_music_artist,
    )
    async_get_music_artist_albums = cast(
        _AsyncMusicListCall, MediaCatalogOwner.async_get_music_artist_albums
    )
    async_get_music_artist_related = cast(
        Callable[..., Awaitable[list[MusicArtistInfo]]],
        MediaCatalogOwner.async_get_music_artist_related,
    )
    _directory_audio_files = classmethod(  # type: ignore[var-annotated]
        MediaAlbumOwner._directory_audio_files.__func__  # type: ignore[attr-defined]
    )
    _album_directory_signature = staticmethod(MediaAlbumOwner._album_directory_signature)
    _music_track_title_key = staticmethod(MediaAlbumOwner._music_track_title_key)
    _align_music_album_tracks = classmethod(  # type: ignore[var-annotated]
        MediaAlbumOwner._align_music_album_tracks.__func__  # type: ignore[attr-defined]
    )
    _match_music_album_directory = MediaAlbumOwner._match_music_album_directory
    _async_match_music_album_directory = MediaAlbumOwner._async_match_music_album_directory
    recognize_music_album_directory = cast(
        _AlbumMappingCall, MediaAlbumOwner.recognize_music_album_directory
    )
    async_recognize_music_album_directory = cast(
        _AsyncAlbumMappingCall,
        MediaAlbumOwner.async_recognize_music_album_directory,
    )
    _merge_tmdb_auxiliary = staticmethod(MediaAuxiliaryOwner._merge_tmdb_auxiliary)
    _build_tmdb_supplement_meta = staticmethod(MediaAuxiliaryOwner._build_tmdb_supplement_meta)
    _media_alias_candidates = staticmethod(MediaAuxiliaryOwner._media_alias_candidates)
    _merge_media_auxiliary = classmethod(  # type: ignore[var-annotated]
        MediaAuxiliaryOwner._merge_media_auxiliary.__func__  # type: ignore[attr-defined]
    )
    _resolve_auxiliary_sources = staticmethod(MediaAuxiliaryOwner._resolve_auxiliary_sources)
    supplement_media_info = cast(
        _MediaOrMusicCall, MediaAuxiliaryOwner.supplement_media_info
    )
    async_supplement_media_info = cast(
        _AsyncMediaOrMusicCall, MediaAuxiliaryOwner.async_supplement_media_info
    )
    supplement_tmdb_info = cast(
        _MediaOrMusicCall, MediaAuxiliaryOwner.supplement_tmdb_info
    )
    is_audio_path = classmethod(  # type: ignore[var-annotated]
        MediaPathOwner.is_audio_path.__func__  # type: ignore[attr-defined]
    )
    read_path_meta = classmethod(  # type: ignore[var-annotated]
        MediaPathOwner.read_path_meta.__func__  # type: ignore[attr-defined]
    )
    _music_info_from_path_meta = classmethod(  # type: ignore[var-annotated]
        MediaPathOwner._music_info_from_path_meta.__func__  # type: ignore[attr-defined]
    )
    _merge_music_audio_quality = staticmethod(MediaPathOwner._merge_music_audio_quality)
    _clear_music_identity = staticmethod(MediaPathOwner._clear_music_identity)
    _is_remote_music_info = staticmethod(MediaPathOwner._is_remote_music_info)
    _recognize_musicbrainz_recording = MediaPathOwner._recognize_musicbrainz_recording
    _async_recognize_musicbrainz_recording = MediaPathOwner._async_recognize_musicbrainz_recording
    _recognize_music_meta_tier = MediaPathOwner._recognize_music_meta_tier
    _async_recognize_music_meta_tier = MediaPathOwner._async_recognize_music_meta_tier
    _music_album_dir_fallback = MediaPathOwner._music_album_dir_fallback
    _async_music_album_dir_fallback = MediaPathOwner._async_music_album_dir_fallback
    recognize_music_by_path = cast(
        Callable[..., tuple[MetaMusic, MusicInfo]],
        MediaPathOwner.recognize_music_by_path,
    )
    async_recognize_music_by_path = cast(
        Callable[..., Awaitable[tuple[MetaMusic, MusicInfo]]],
        MediaPathOwner.async_recognize_music_by_path,
    )
    _is_music_path_request = MediaPathOwner._is_music_path_request
    recognize_by_path = cast(_ContextCall, MediaPathOwner.recognize_by_path)
    async_recognize_by_path = cast(
        _AsyncContextCall, MediaPathOwner.async_recognize_by_path
    )
    select_recognize_source = cast(
        _MediaCall, MediaPluginOwner.select_recognize_source
    )
    _parse_recognize_event_number = staticmethod(MediaPluginOwner._parse_recognize_event_number)
    recognize_help = cast(_MediaCall, MediaPluginOwner.recognize_help)
    _recognize_music_help = MediaPluginOwner._recognize_music_help
    _parse_music_recognize_event = staticmethod(MediaPluginOwner._parse_music_recognize_event)
    _build_music_help_meta = staticmethod(MediaPluginOwner._build_music_help_meta)
    async_select_recognize_source = cast(
        _AsyncMediaCall, MediaPluginOwner.async_select_recognize_source
    )
    async_recognize_help = cast(
        _AsyncMediaCall, MediaPluginOwner.async_recognize_help
    )
    _async_recognize_music_help = MediaPluginOwner._async_recognize_music_help
    convert_media_identity = cast(
        _ProjectionCall, MediaProjectionOwner.convert_media_identity
    )
    _dispatch_projection_event = MediaProjectionOwner._dispatch_projection_event
    _extract_year_from_bangumi = staticmethod(MediaProjectionOwner._extract_year_from_bangumi)
    _extract_year_from_tmdb = staticmethod(MediaProjectionOwner._extract_year_from_tmdb)
    _match_tmdb_with_names = MediaProjectionOwner._match_tmdb_with_names
    _async_match_tmdb_with_names = MediaProjectionOwner._async_match_tmdb_with_names
    async_convert_media_identity = cast(
        _AsyncProjectionCall, MediaProjectionOwner.async_convert_media_identity
    )
    _async_dispatch_projection_event = (
        MediaProjectionOwner._async_dispatch_projection_event
    )
    _run_native_media_recognize = MediaRecognitionOwner._run_native_media_recognize
    _async_run_native_media_recognize = MediaRecognitionOwner._async_run_native_media_recognize
    recognize_by_meta = cast(_MediaCall, MediaRecognitionOwner.recognize_by_meta)
    _recognize_with_fallback_by_meta = MediaRecognitionOwner._recognize_with_fallback_by_meta
    async_recognize_by_meta = cast(
        _AsyncMediaCall, MediaRecognitionOwner.async_recognize_by_meta
    )
    _async_recognize_with_fallback_by_meta = MediaRecognitionOwner._async_recognize_with_fallback_by_meta
    search = cast(_SearchCall, MediaSearchOwner.search)
    async_search = cast(_AsyncSearchCall, MediaSearchOwner.async_search)
