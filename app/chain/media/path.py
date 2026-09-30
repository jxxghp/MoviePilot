"""音频证据、单曲层级与统一路径识别 owner。"""

import re
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Generator, Optional, Tuple, TypeGuard, Union, cast

from app.application.audio import AudioMetadataHelper
from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.music.observation import (
    capture_music_recognition,
    music_recognition_failed,
    music_recognition_is_blocked,
    report_music_fingerprints,
)
from app.chain.acoustid import AcoustIdChain
from app.chain.media.contract import _MediaOwnerBase
from app.domain.context import (
    Context,
    MediaInfo,
    MusicInfo,
)
from app.domain.media import is_music_media_source
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfoPath
from app.domain.music import MusicDirectoryMatch, music_album_title_is_weak, music_track_title_is_weak
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.schemas.media import normalize_media_source
from app.schemas.types import (
    MUSIC_ENTITY_RECORDING,
    MediaSource,
)

_FINGERPRINT_TITLE_QUALIFIER = re.compile(
    r"\s*[\[(（【][^\])）】]*(?:radio|single|version|edit|mix|remix|remaster(?:ed)?|"
    r"live|acoustic|demo|mono|stereo|recorded|pop)[^\])）】]*[\])）】]",
    re.IGNORECASE,
)
_CONTENT_RATING_QUALIFIER = re.compile(
    r"\s*[\[(（【]\s*(?:explicit|clean)\s*[\])）】]",
    re.IGNORECASE,
)
_SINGLE_RELEASE_SUFFIX = re.compile(
    r"\s*[-–—]\s*(?:single|单曲)\s*$",
    re.IGNORECASE,
)


def _is_regular_file(path: Path) -> bool:
    """判断路径是否仍指向可读取的普通文件。"""
    return path.exists() and path.is_file()


class _MusicTierActionKind(Enum):
    """音乐证据层允许执行的外部识别动作。"""

    DIRECT = "direct"
    SEARCH = "search"


@dataclass(frozen=True, slots=True)
class _MusicTierAction:
    """描述一次音乐证据层 I/O，不携带可变执行状态。"""

    kind: _MusicTierActionKind
    meta: MetaMusic
    recording_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class _MusicTierOutcome:
    """保存证据层命中结果及其日志说明。"""

    info: Optional[MusicInfo] = None
    message: Optional[str] = None


class _MusicPathActionKind(Enum):
    """音乐路径识别状态机的稳定动作顺序。"""

    FINGERPRINT = "fingerprint"
    TAG = "tag"
    FILENAME = "filename"
    ALBUM = "album"


@dataclass(frozen=True, slots=True)
class _MusicPathAction:
    """描述音乐路径状态机的一层识别动作。"""

    kind: _MusicPathActionKind
    meta: Optional[MetaMusic] = None
    tier_name: Optional[str] = None


class _PathRoute(Enum):
    """统一路径识别入口的业务路由。"""

    MUSIC = "music"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class _PathRecognitionRequest:
    """保存同步和异步路径识别共同使用的请求参数。"""

    path: str
    route: _PathRoute
    media_source: Optional[MediaSource]
    episode_group: Optional[str]
    obtain_images: bool


def _has_remote_music_identity(
    info: Optional[MusicInfo],
) -> TypeGuard[MusicInfo]:
    """判断音乐结果是否已获得可终止回退的远程身份。"""
    return bool(info and info.media_source and info.media_id)


def _musicbrainz_recording_meta(meta: MetaMusic, recording_id: str) -> MetaMusic:
    """构造 MusicBrainz Recording 直查使用的独立身份副本。"""
    identity_meta = MetaMusic.from_dict(meta.to_dict())
    identity_meta.media_source = MediaSource.MusicBrainz
    identity_meta.media_id = recording_id
    identity_meta.music_type = MUSIC_ENTITY_RECORDING
    return identity_meta


def _without_music_identity(meta: MetaMusic) -> MetaMusic:
    """复制音乐元数据并移除可能误导标题搜索的远程身份。"""
    clean_meta = MetaMusic.from_dict(meta.to_dict())
    clean_meta.media_source = None
    clean_meta.media_id = None
    return clean_meta


def _merge_music_path_evidence(
    meta: MetaMusic,
    evidence: Optional[MetaMusic],
) -> MetaMusic:
    """把已确认的标签证据补入文件名搜索，但不传播远程身份。"""
    if not meta or not evidence:
        return meta
    merged = MetaMusic.from_dict(meta.to_dict())
    if not merged.artists and evidence.artists:
        merged.artists = list(evidence.artists)
    if not merged.album_artist and evidence.album_artist:
        merged.album_artist = evidence.album_artist
    if not merged.album and evidence.album:
        merged.album = evidence.album
    if not merged.year and evidence.year:
        merged.year = evidence.year
    if not merged.version and evidence.version:
        merged.version = evidence.version
    return merged


def _merge_contextual_music_evidence(
    meta: MetaMusic,
    contextual_meta: MetaMusic,
) -> MetaMusic:
    """以同目录高置信共识补齐缺失的艺人和专辑证据。"""
    merged = MetaMusic.from_dict(meta.to_dict())
    if not merged.artists and contextual_meta.artists:
        merged.artists = list(contextual_meta.artists)
    if not merged.album_artist and contextual_meta.album_artist:
        merged.album_artist = contextual_meta.album_artist
    if not merged.album and contextual_meta.album:
        merged.album = contextual_meta.album
    if contextual_meta.year:
        # 整理链会先用最近的发行目录年份纠正下载包中的旧标签年份；这里
        # 必须保留该强上下文，否则重新读取音频标签后又会退回创作年或旧版年。
        merged.year = contextual_meta.year
    if not merged.version and contextual_meta.version:
        merged.version = contextual_meta.version
    return merged


def _merge_music_audio_quality(info: MusicInfo, meta: MetaMusic) -> MusicInfo:
    """将本地文件的实际音频参数合并到音乐识别结果。"""
    for key in (
        "audio_format",
        "audio_lossless",
        "bit_depth",
        "sample_rate",
        "bitrate",
    ):
        value = getattr(meta, key, None)
        if value is not None:
            setattr(info, key, value)
            if key in meta.field_sources:
                info.field_sources[key] = meta.field_sources[key]
    return info


def _finalize_music_path_info(
    meta: MetaMusic,
    info: Optional[MusicInfo],
) -> MusicInfo:
    """统一远端命中和本地兜底的音频质量合并。"""
    if info and music_recognition_is_blocked(info) and not _has_remote_music_identity(info):
        result = MusicInfo.from_meta(meta)
        result.raw_data["recognition"] = deepcopy(info.raw_data["recognition"])
    else:
        result = _merge_music_audio_quality(info or MusicInfo.from_meta(meta), meta)
    if (
        _has_remote_music_identity(result)
        and result.music_type == MUSIC_ENTITY_RECORDING
        and not result.album_type
    ):
        # 部分 MusicBrainz Recording 没有关联 Release Group 类型，统一按单曲归类。
        result.album_type = "Single"
    return result


def _fingerprint_info_matches_evidence(
    info: Optional[MusicInfo],
    tag_meta: Optional[MetaMusic],
    filename_meta: Optional[MetaMusic],
    *,
    fingerprint_score: Optional[float] = None,
    fingerprint_duration: Optional[int] = None,
) -> bool:
    """保留强标签冲突检查；原生高分指纹可用真实时长弥补占位名称或缺失署名。"""
    from app.domain.music import (  # pylint: disable=import-outside-toplevel
        music_album_matches,
        music_artist_matches,
        music_text_key,
        music_usable_artists,
        music_version_matches,
        music_year_matches,
    )

    if not _has_remote_music_identity(info):
        return False
    primary = tag_meta if tag_meta and not music_track_title_is_weak(tag_meta) else filename_meta or tag_meta
    if not primary:
        return False
    weak_title = music_track_title_is_weak(primary)
    local_duration = fingerprint_duration or (tag_meta.duration if tag_meta else None) or primary.duration
    close_duration = bool(local_duration and info.duration and abs(local_duration - info.duration) <= max(3, min(8, local_duration * 0.025)))
    native_strong = fingerprint_score is not None and fingerprint_score >= 0.98 and close_duration
    if local_duration and info.duration and abs(local_duration - info.duration) > max(10, local_duration * 0.08):
        return False
    artist_meta = tag_meta if tag_meta and tag_meta.artists else primary
    artist_evidence = music_usable_artists(artist_meta.artists) or music_usable_artists(
        filename_meta.artists if filename_meta else []
    )
    collective = {music_text_key(value) for value in ("Various Artists", "Various", "VA", "群星", "众艺人", "眾藝人")}
    if (fingerprint_score is not None and artist_meta.field_sources.get("artists") in {"directory", "torrent", "album_tags"}
            and all(music_text_key(artist) in collective for artist in artist_evidence)):
        artist_evidence = []
    if not artist_evidence and not native_strong:
        return False
    if artist_evidence and not music_artist_matches(info, artist_evidence):
        return False
    if (not weak_title or primary.version) and not music_version_matches(info, primary):
        return False
    if tag_meta and tag_meta is not primary and tag_meta.version and not music_version_matches(info, tag_meta):
        return False
    standalone_single = _is_standalone_single_evidence(primary)
    if (
        primary.album
        and info.album
        and not music_album_matches(info, primary.album)
        and not standalone_single
        and not native_strong
    ):
        return False
    if not native_strong and not standalone_single and not music_year_matches(info, primary):
        return False
    if weak_title:
        return native_strong
    return _fingerprint_title_matches(info, primary.title)


def _fingerprint_title_matches(info: MusicInfo, title_evidence: str) -> bool:
    """在音频与署名已核验后比较曲名别称及展示差异，避免混入发行身份判断。"""
    from difflib import SequenceMatcher

    from app.domain.music import (  # pylint: disable=import-outside-toplevel
        music_base_title,
        music_text_key,
        music_title_matches,
        music_titles,
    )

    if music_title_matches(info, title_evidence):
        return True
    # 上层已核验音频、艺人及版本，允许常见展示后缀与轻微标签拼写差异。
    evidence_key = music_text_key(music_base_title(title_evidence))
    version_suffix = re.compile(
        r"(?:radio|single|version|edit|mix|remix|remaster(?:ed)?|live|acoustic|"
        r"demo|mono|stereo|recorded)+"
    )
    for title in music_titles(info):
        candidate_key = music_text_key(music_base_title(title))
        if not candidate_key:
            continue
        shorter, longer = sorted((candidate_key, evidence_key), key=len)
        if shorter and longer.startswith(shorter) and version_suffix.fullmatch(longer[len(shorter):]):
            return True
        qualified_evidence_key = music_text_key(
            music_base_title(_FINGERPRINT_TITLE_QUALIFIER.sub("", title_evidence))
        )
        qualified_candidate_key = music_text_key(
            music_base_title(_FINGERPRINT_TITLE_QUALIFIER.sub("", title))
        )
        if qualified_evidence_key and qualified_evidence_key == qualified_candidate_key:
            return True
        if SequenceMatcher(None, candidate_key, evidence_key).ratio() >= 0.82:
            return True
    return False


def _is_standalone_single_evidence(meta: MetaMusic) -> bool:
    """判断本地标签是否明确把当前录音描述为同名单曲发行。"""
    from app.domain.music import (  # pylint: disable=import-outside-toplevel
        music_base_title,
        music_text_key,
    )

    title_key = music_text_key(
        music_base_title(
            _SINGLE_RELEASE_SUFFIX.sub(
                "",
                _CONTENT_RATING_QUALIFIER.sub("", meta.title or ""),
            )
        )
    )
    album_key = music_text_key(
        music_base_title(
            _SINGLE_RELEASE_SUFFIX.sub(
                "",
                _CONTENT_RATING_QUALIFIER.sub("", meta.album or ""),
            )
        )
    )
    return bool(title_key and album_key and title_key == album_key)


def _reconcile_fingerprint_release(
    info: MusicInfo,
    tag_meta: Optional[MetaMusic],
    filename_meta: Optional[MetaMusic],
    *,
    verified_audio: bool = False,
) -> MusicInfo:
    """指纹只确认录音，用本地发行证据校正远端随意选取的关联专辑。

    MusicBrainz Recording 可以同时收录于单曲和原声专辑。指纹证明的是录音
    身份，不能确认实际发行；采用本地专辑字段时移除没有证明的发行标识，
    保留字段来源，旧插件的单ID回执仍只适用原有同名单曲纠正。
    """
    from app.domain.music import (  # pylint: disable=import-outside-toplevel
        music_album_matches,
        music_year_matches,
    )
    primary = tag_meta if tag_meta and tag_meta.title else filename_meta
    if (
        not primary
        or not (_is_standalone_single_evidence(primary) or verified_audio)
        or not primary.album
        or music_album_title_is_weak(primary)
        or (
            music_album_matches(info, primary.album)
            and music_year_matches(info, primary)
        )
    ):
        return info

    reconciled = deepcopy(info)
    reconciled.album = primary.album
    reconciled.album_artist = primary.album_artist or info.artist
    reconciled.album_id = None
    reconciled.musicbrainz_release_id = primary.musicbrainz_release_id
    reconciled.musicbrainz_release_group_id = primary.musicbrainz_release_group_id
    reconciled.musicbrainz_release_track_id = primary.musicbrainz_release_track_id
    reconciled.release_year = primary.release_year
    reconciled.original_year = primary.original_year or info.original_year
    reconciled.album_type = primary.album_type or ("Single" if _is_standalone_single_evidence(primary) else None)
    reconciled.secondary_types = list(primary.secondary_types)
    reconciled.year = int(primary.year) if primary.year is not None else None
    reconciled.release_date = None
    reconciled.release_status = None
    reconciled.disc_number = primary.disc_number
    reconciled.track_number = primary.track_number
    reconciled.total_tracks = primary.total_tracks
    reconciled.total_discs = primary.total_discs
    reconciled.album_aliases = []
    reconciled.cover_url = None
    reconciled.category = ""
    reconciled.metadata_category = reconciled.album_type or ""
    reconciled.classification = None
    for key in ("album", "album_artist", "year", "release_year", "disc_number", "track_number", "total_tracks", "total_discs",
                "musicbrainz_release_id", "musicbrainz_release_group_id", "musicbrainz_release_track_id"):
        if getattr(reconciled, key, None) is not None:
            reconciled.field_sources[key] = primary.field_sources.get(key, "tag" if primary is tag_meta else "filename")
        else:
            reconciled.field_sources.pop(key, None)
    return reconciled


def _select_fingerprint_info(
        meta: MetaMusic, matches: list[tuple[Optional[float], MusicInfo]],
        tag_meta: Optional[MetaMusic], filename_meta: Optional[MetaMusic],
        *, truncated: bool = False,
) -> Optional[MusicInfo]:
    """只采用唯一可解释的录音候选，近分多录音保留待确认，不用文本回退掩盖歧义。"""
    if not matches:
        return None
    if tag_meta and tag_meta.media_source == MediaSource.MusicBrainz and tag_meta.media_id:
        tagged = next(((score, info) for score, info in matches if info.media_id == tag_meta.media_id), None)
        if tagged is None:
            return None
        score, info = tagged
        return _finish_fingerprint_recording(score, info, tag_meta, filename_meta)
    ranked = sorted(matches, key=lambda item: item[0] if item[0] is not None else 1.0, reverse=True)
    if truncated or (len(ranked) > 1 and round((ranked[0][0] or 0) - (ranked[1][0] or 0), 6) <= 0.05):
        pending = MusicInfo.from_meta(meta)
        pending.media_source, pending.media_id = None, None
        pending.raw_data["recognition"] = {"status": "ambiguous", "message": "指纹对应多个合理的录音，请手动选择歌曲或专辑", "candidates": [
            {"media_source": str(info.media_source), "media_id": info.media_id, "title": info.title, "artist": info.artist, "score": score}
            for score, info in ranked[:5]
        ]}
        return pending
    score, info = ranked[0]
    return _finish_fingerprint_recording(score, info, tag_meta, filename_meta)


def _finish_fingerprint_recording(
        score: Optional[float], info: MusicInfo, tag_meta: Optional[MetaMusic], filename_meta: Optional[MetaMusic],
) -> MusicInfo:
    """仅确认Recording；发行ID只能沿用实际标签，不能从指纹详情所选关联发行推断。"""
    if score is None:
        # 旧插件的单ID回执沿用既有对象及文本核验契约，不伪造原生指纹结论。
        return _reconcile_fingerprint_release(info, tag_meta, filename_meta)
    result = deepcopy(_reconcile_fingerprint_release(info, tag_meta, filename_meta,
                                                     verified_audio=score is not None and score >= 0.98))
    result.album_id = tag_meta.musicbrainz_release_group_id if tag_meta else None
    result.field_sources.pop("album_id", None)
    for key in ("musicbrainz_release_id", "musicbrainz_release_group_id", "musicbrainz_release_track_id"):
        value = getattr(tag_meta, key, None)
        setattr(result, key, value)
        if value:
            result.field_sources[key] = tag_meta.field_sources.get(key, "tag") if tag_meta else "tag"
        else:
            result.field_sources.pop(key, None)
    result.raw_data["recognition"] = {"status": "matched", "method": "fingerprint", "score": score,
                                       "identity_type": "recording", "release_verified": False}
    return result


def _fingerprint_candidate_list(recording_id: Optional[str], observed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """旧插件仍只给ID时保持原核验要求，不给它制造AcoustID分数。"""
    if observed:
        return observed[:5]
    return [{"recording_id": recording_id, "score": None, "duration": None}] if recording_id else []


def _matching_fingerprint(
        candidate: dict[str, Any], info: Optional[MusicInfo],
        tag_meta: Optional[MetaMusic], filename_meta: Optional[MetaMusic],
) -> bool:
    """同步和异步候选采用同一证据核验规则。"""
    return _fingerprint_info_matches_evidence(info, tag_meta, filename_meta,
                                             fingerprint_score=candidate.get("score"),
                                             fingerprint_duration=candidate.get("duration"))


def _recognize_fingerprints(
        path: Union[str, Path], meta: MetaMusic, tag_meta: Optional[MetaMusic], filename_meta: Optional[MetaMusic],
        recognize: Callable[[MetaMusic, str], Optional[MusicInfo]],
) -> Optional[MusicInfo]:
    """在一次有界调用中逐个核验原生候选，保留旧指纹插件的调用顺序。"""
    with capture_music_recognition(seconds=90, request_limit=16) as observation:
        report_music_fingerprints([])
        recording_id = AcoustIdChain().identify_music_by_fingerprint(path)
        matches: list[tuple[Optional[float], MusicInfo]] = []
        for candidate in _fingerprint_candidate_list(recording_id, observation.fingerprint_candidates):
            info = recognize(meta, str(candidate["recording_id"]))
            if info and _matching_fingerprint(candidate, info, tag_meta, filename_meta):
                matches.append((candidate.get("score"), info))
        return None if music_recognition_failed() else _select_fingerprint_info(
            meta, matches, tag_meta, filename_meta, truncated=any(item.get("truncated") for item in observation.fingerprint_candidates),
        )


async def _async_recognize_fingerprints(
        path: Union[str, Path], meta: MetaMusic, tag_meta: Optional[MetaMusic], filename_meta: Optional[MetaMusic],
        recognize: Callable[[MetaMusic, str], Awaitable[Optional[MusicInfo]]],
) -> Optional[MusicInfo]:
    """异步路径使用相同候选集合、预算和最终歧义判断。"""
    with capture_music_recognition(seconds=90, request_limit=16) as observation:
        report_music_fingerprints([])
        recording_id = await AcoustIdChain().async_identify_music_by_fingerprint(path)
        matches: list[tuple[Optional[float], MusicInfo]] = []
        for candidate in _fingerprint_candidate_list(recording_id, observation.fingerprint_candidates):
            info = await recognize(meta, str(candidate["recording_id"]))
            if info and _matching_fingerprint(candidate, info, tag_meta, filename_meta):
                matches.append((candidate.get("score"), info))
        return None if music_recognition_failed() else _select_fingerprint_info(
            meta, matches, tag_meta, filename_meta, truncated=any(item.get("truncated") for item in observation.fingerprint_candidates),
        )


def _music_info_matches_text_evidence(
    info: Optional[MusicInfo],
    meta: Optional[MetaMusic],
) -> bool:
    """统一校验各识别层都必须遵守的版本和发行年份证据。"""
    from app.domain.music import (  # pylint: disable=import-outside-toplevel
        music_version_matches,
        music_year_matches,
    )

    if not _has_remote_music_identity(info) or not meta:
        return False
    return bool(music_version_matches(info, meta) and music_year_matches(info, meta))


def _music_tier_plan(
    meta: Optional[MetaMusic],
    media_source: Optional[MediaSource],
    tier_name: str,
) -> Generator[_MusicTierAction, Optional[MusicInfo], _MusicTierOutcome]:
    """按 MBID 直查再标题搜索的固定顺序生成证据层动作。"""
    if not meta:
        return _MusicTierOutcome()
    normalized_source = normalize_media_source(media_source)
    search_meta = meta
    if meta.media_source == MediaSource.MusicBrainz and meta.media_id:
        if normalized_source in (None, MediaSource.MusicBrainz):
            direct = yield _MusicTierAction(
                kind=_MusicTierActionKind.DIRECT,
                meta=meta,
                recording_id=str(meta.media_id),
            )
            if _has_remote_music_identity(direct):
                return _MusicTierOutcome(
                    info=direct,
                    message=f"音乐识别命中{tier_name}层 MusicBrainz ID 直查",
                )
            if music_recognition_is_blocked(direct):
                return _MusicTierOutcome(info=direct, message="音乐识别需要重试或确认")
        search_meta = _without_music_identity(meta)
    if not search_meta.title:
        return _MusicTierOutcome()
    result = yield _MusicTierAction(
        kind=_MusicTierActionKind.SEARCH,
        meta=search_meta,
    )
    if music_recognition_is_blocked(result):
        return _MusicTierOutcome(info=result, message="音乐识别需要重试或确认")
    if _has_remote_music_identity(result):
        return _MusicTierOutcome(
            info=result,
            message=f"音乐识别命中{tier_name}层：{result.title}",
        )
    return _MusicTierOutcome()


def _music_path_plan(
    tag_meta: Optional[MetaMusic],
    filename_meta: Optional[MetaMusic],
    media_source: Optional[MediaSource],
) -> Generator[_MusicPathAction, Optional[MusicInfo], Optional[MusicInfo]]:
    """生成“指纹→标签→文件名→专辑目录”的唯一回退状态机。"""
    normalized_source = normalize_media_source(media_source)
    if normalized_source in (None, MediaSource.MusicBrainz):
        info = yield _MusicPathAction(kind=_MusicPathActionKind.FINGERPRINT)
        if _has_remote_music_identity(info) or music_recognition_is_blocked(info):
            return info
    info = yield _MusicPathAction(
        kind=_MusicPathActionKind.TAG,
        meta=tag_meta,
        tier_name="文件标签",
    )
    if _has_remote_music_identity(info) or music_recognition_is_blocked(info):
        return info
    info = yield _MusicPathAction(
        kind=_MusicPathActionKind.FILENAME,
        meta=filename_meta,
        tier_name="文件名",
    )
    if _has_remote_music_identity(info) or music_recognition_is_blocked(info):
        return info
    if normalized_source in (None, MediaSource.MusicBrainz):
        return (yield _MusicPathAction(kind=_MusicPathActionKind.ALBUM))
    return None


def _build_path_recognition_request(
    path: str,
    media_source: Optional[MediaSource],
    episode_group: Optional[str],
    obtain_images: bool,
    is_music: bool,
) -> _PathRecognitionRequest:
    """将路径识别参数投影为同步和异步共用的稳定路由请求。"""
    return _PathRecognitionRequest(
        path=path,
        route=_PathRoute.MUSIC if is_music else _PathRoute.VIDEO,
        media_source=media_source,
        episode_group=episode_group,
        obtain_images=obtain_images,
    )


def _path_context(
    meta: Any,
    info: Optional[Union[MediaInfo, MusicInfo]],
) -> Context:
    """统一成功与失败路径的 Context 投影。"""
    if info is not None:
        return Context(meta_info=meta, media_info=info)
    return Context(meta_info=meta)


class MediaPathOwner(_MediaOwnerBase):
    """音频证据、单曲层级与统一路径识别 owner。"""

    @classmethod
    def is_audio_path(cls, path: Union[str, Path]) -> bool:
        """判断路径是否指向系统支持的音频文件。"""
        return Path(path).suffix.lower() in get_chain_runtime_config_snapshot().audio_extensions

    @classmethod
    def read_path_meta(cls, path: Union[str, Path], *, storage: Optional[str] = "local") -> MetaMusic:
        """本地文件读取音频证据；远端只解析名称，不能借用本机同名路径。"""
        file_path = Path(path)
        if storage == "local" and file_path.is_file():
            return AudioMetadataHelper.read(file_path)
        return AudioMetadataHelper.read_filename(file_path)

    @classmethod
    def _music_info_from_path_meta(cls, meta: MetaMusic) -> MusicInfo:
        """把音频标签转换为文件管理可展示的最小音乐信息。"""
        return MusicInfo.from_meta(meta)

    @staticmethod
    def _merge_music_audio_quality(info: MusicInfo, meta: MetaMusic) -> MusicInfo:
        """将本地文件的实际音频参数合并到远端音乐身份识别结果。"""
        return _merge_music_audio_quality(info, meta)

    @staticmethod
    def _clear_music_identity(meta: MetaMusic) -> MetaMusic:
        """复制音乐元数据并清除远程身份，供直查失败后按要素重新匹配。"""
        return _without_music_identity(meta)

    @staticmethod
    def _is_remote_music_info(info: Optional[MusicInfo]) -> TypeGuard[MusicInfo]:
        """判断音乐识别结果是否携带可复用的远程身份。"""
        return _has_remote_music_identity(info)

    def _recognize_musicbrainz_recording(
        self,
        meta: MetaMusic,
        recording_id: str,
    ) -> Optional[MusicInfo]:
        """按已知 MusicBrainz Recording ID 直接读取单曲详情。"""
        identity_meta = _musicbrainz_recording_meta(meta, recording_id)
        return self.recognize_music_from_source(
            media_source=MediaSource.MusicBrainz,
            meta=identity_meta,
            media_id=recording_id,
            music_type=MUSIC_ENTITY_RECORDING,
        )

    async def _async_recognize_musicbrainz_recording(
        self,
        meta: MetaMusic,
        recording_id: str,
    ) -> Optional[MusicInfo]:
        """异步按已知 MusicBrainz Recording ID 直接读取单曲详情。"""
        identity_meta = _musicbrainz_recording_meta(meta, recording_id)
        return await self.async_recognize_music_from_source(
            media_source=MediaSource.MusicBrainz,
            meta=identity_meta,
            media_id=recording_id,
            music_type=MUSIC_ENTITY_RECORDING,
        )

    def _recognize_music_meta_tier(
        self,
        meta: Optional[MetaMusic],
        media_source: Optional[MediaSource],
        tier_name: str,
    ) -> Optional[MusicInfo]:
        """识别单个音乐元数据证据层，标签中的 MBID 优先直查。"""
        plan = _music_tier_plan(meta, media_source, tier_name)
        outcome = _MusicTierOutcome()
        try:
            action = next(plan)
            while True:
                if action.kind is _MusicTierActionKind.DIRECT:
                    result = self._recognize_musicbrainz_recording(
                        meta=action.meta,
                        recording_id=action.recording_id or "",
                    )
                else:
                    candidate = self.recognize_media(
                        meta=action.meta,
                        media_source=media_source,
                        music_type=MUSIC_ENTITY_RECORDING,
                    )
                    result = candidate if isinstance(candidate, MusicInfo) else None
                action = plan.send(result)
        except StopIteration as completed:
            outcome = cast(_MusicTierOutcome, completed.value)
        if music_recognition_is_blocked(outcome.info):
            return outcome.info
        if outcome.info and not _music_info_matches_text_evidence(outcome.info, meta):
            logger.warning(
                f"{tier_name}音乐候选与本地版本或发行年份冲突，已忽略："
                f"{outcome.info.artist} - {outcome.info.title} ({outcome.info.year or '-'})"
            )
            return None
        if outcome.message:
            logger.info(outcome.message)
        return outcome.info

    async def _async_recognize_music_meta_tier(
        self,
        meta: Optional[MetaMusic],
        media_source: Optional[MediaSource],
        tier_name: str,
    ) -> Optional[MusicInfo]:
        """异步识别单个音乐元数据证据层，标签中的 MBID 优先直查。"""
        plan = _music_tier_plan(meta, media_source, tier_name)
        outcome = _MusicTierOutcome()
        try:
            action = next(plan)
            while True:
                if action.kind is _MusicTierActionKind.DIRECT:
                    result = await self._async_recognize_musicbrainz_recording(
                        meta=action.meta,
                        recording_id=action.recording_id or "",
                    )
                else:
                    candidate = await self.async_recognize_media(
                        meta=action.meta,
                        media_source=media_source,
                        music_type=MUSIC_ENTITY_RECORDING,
                    )
                    result = candidate if isinstance(candidate, MusicInfo) else None
                action = plan.send(result)
        except StopIteration as completed:
            outcome = cast(_MusicTierOutcome, completed.value)
        if music_recognition_is_blocked(outcome.info):
            return outcome.info
        if outcome.info and not _music_info_matches_text_evidence(outcome.info, meta):
            logger.warning(
                f"{tier_name}音乐候选与本地版本或发行年份冲突，已忽略："
                f"{outcome.info.artist} - {outcome.info.title} ({outcome.info.year or '-'})"
            )
            return None
        if outcome.message:
            logger.info(outcome.message)
        return outcome.info

    def _music_album_dir_fallback(
        self,
        path: Union[str, Path],
    ) -> Optional[MusicInfo]:
        """单曲识别无远端身份时，查找所在目录专辑匹配中属于当前文件的结果。"""
        file_path = Path(path)
        if not file_path.exists() or not file_path.is_file():
            return None
        try:
            matched = self.recognize_music_album_directory(file_path.parent)
        except Exception as err:
            logger.debug(f"专辑目录匹配失败：{file_path.parent} - {err}")
            return None
        if isinstance(matched, MusicDirectoryMatch):
            pending = MusicInfo(raw_data={"recognition": deepcopy(matched.recognition)})
            if music_recognition_is_blocked(pending):
                return pending
        return matched.get(str(file_path.resolve()))

    async def _async_music_album_dir_fallback(
        self,
        path: Union[str, Path],
    ) -> Optional[MusicInfo]:
        """异步查找所在目录专辑匹配中属于当前文件的结果。"""
        file_path = Path(path)
        if not await run_in_threadpool(_is_regular_file, file_path):
            return None
        try:
            matched = await self.async_recognize_music_album_directory(file_path.parent)
        except Exception as err:
            logger.debug(f"专辑目录匹配失败：{file_path.parent} - {err}")
            return None
        if isinstance(matched, MusicDirectoryMatch):
            pending = MusicInfo(raw_data={"recognition": deepcopy(matched.recognition)})
            if music_recognition_is_blocked(pending):
                return pending
        return matched.get(str(file_path.resolve()))

    def recognize_music_by_path(
        self,
        path: Union[str, Path],
        media_source: Optional[MediaSource] = None,
        contextual_meta: Optional[MetaMusic] = None,
    ) -> Tuple[MetaMusic, MusicInfo]:
        """按指纹、文件标签、文件名三级顺序识别本地音乐。"""
        meta, tag_meta, filename_meta = AudioMetadataHelper.read_evidence(Path(path))
        if meta.music_layout == "image_cue" or meta.organization_error:
            # 整轨包含多个逻辑音轨，不能把开头的指纹 Recording 当作整个文件。
            cue_info = MusicInfo.from_meta(meta)
            return meta, cast(MusicInfo, self._finalize_recognition_result(cue_info, allow_enrichment=False))
        filename_meta = _merge_music_path_evidence(filename_meta, meta)
        if contextual_meta:
            meta = _merge_contextual_music_evidence(meta, contextual_meta)
            if tag_meta:
                tag_meta = _merge_contextual_music_evidence(tag_meta, contextual_meta)
            filename_meta = _merge_contextual_music_evidence(filename_meta, contextual_meta)
        # 文件名层只负责提供曲名；即使调用方没有显式传目录上下文，也必须
        # 继承 read_evidence 已确认的标签艺人、专辑、年份和版本，避免标签层
        # 临时请求失败后退化成无约束的全库同名搜索。
        filename_meta = _merge_contextual_music_evidence(filename_meta, meta)
        plan = _music_path_plan(tag_meta, filename_meta, media_source)
        info: Optional[MusicInfo] = None
        try:
            action = next(plan)
            while True:
                if action.kind is _MusicPathActionKind.FINGERPRINT:
                    info = _recognize_fingerprints(path, meta, tag_meta, filename_meta, self._recognize_musicbrainz_recording)
                elif action.kind is _MusicPathActionKind.ALBUM:
                    info = self._music_album_dir_fallback(path)
                    if info and not music_recognition_is_blocked(info) and not _music_info_matches_text_evidence(info, meta):
                        logger.warning(
                            "音乐目录候选与本地版本或发行年份冲突，已忽略："
                            f"{Path(path).name} -> {info.artist} - {info.album or info.title} "
                            f"({info.year or '-'})"
                        )
                        info = None
                else:
                    info = self._recognize_music_meta_tier(
                        meta=action.meta,
                        media_source=media_source,
                        tier_name=action.tier_name or "",
                    )
                action = plan.send(info)
        except StopIteration as completed:
            info = cast(Optional[MusicInfo], completed.value)
        result = _finalize_music_path_info(meta, info)
        simplified = self._simplify_recognized_music_info(result)
        return meta, cast(
            MusicInfo,
            self._finalize_recognition_result(simplified),
        )

    async def async_recognize_music_by_path(
        self,
        path: Union[str, Path],
        media_source: Optional[MediaSource] = None,
        contextual_meta: Optional[MetaMusic] = None,
    ) -> Tuple[MetaMusic, MusicInfo]:
        """异步按指纹、文件标签、文件名三级顺序识别本地音乐。"""
        meta, tag_meta, filename_meta = await run_in_threadpool(
            AudioMetadataHelper.read_evidence,
            Path(path),
        )
        if meta.music_layout == "image_cue" or meta.organization_error:
            cue_info = MusicInfo.from_meta(meta)
            return meta, cast(MusicInfo, await self._async_finalize_recognition_result(cue_info, allow_enrichment=False))
        filename_meta = _merge_music_path_evidence(filename_meta, meta)
        if contextual_meta:
            meta = _merge_contextual_music_evidence(meta, contextual_meta)
            if tag_meta:
                tag_meta = _merge_contextual_music_evidence(tag_meta, contextual_meta)
            filename_meta = _merge_contextual_music_evidence(filename_meta, contextual_meta)
        filename_meta = _merge_contextual_music_evidence(filename_meta, meta)
        plan = _music_path_plan(tag_meta, filename_meta, media_source)
        info: Optional[MusicInfo] = None
        try:
            action = next(plan)
            while True:
                if action.kind is _MusicPathActionKind.FINGERPRINT:
                    info = await _async_recognize_fingerprints(path, meta, tag_meta, filename_meta, self._async_recognize_musicbrainz_recording)
                elif action.kind is _MusicPathActionKind.ALBUM:
                    info = await self._async_music_album_dir_fallback(path)
                    if info and not music_recognition_is_blocked(info) and not _music_info_matches_text_evidence(info, meta):
                        logger.warning(
                            "音乐目录候选与本地版本或发行年份冲突，已忽略："
                            f"{Path(path).name} -> {info.artist} - {info.album or info.title} "
                            f"({info.year or '-'})"
                        )
                        info = None
                else:
                    info = await self._async_recognize_music_meta_tier(
                        meta=action.meta,
                        media_source=media_source,
                        tier_name=action.tier_name or "",
                    )
                action = plan.send(info)
        except StopIteration as completed:
            info = cast(Optional[MusicInfo], completed.value)
        result = _finalize_music_path_info(meta, info)
        simplified = self._simplify_recognized_music_info(result)
        return meta, cast(
            MusicInfo,
            await self._async_finalize_recognition_result(simplified),
        )

    def _is_music_path_request(self, path: str, media_source: Optional[MediaSource]) -> bool:
        """路径识别请求是否属于音乐：音频后缀文件或显式指定音乐数据源。"""
        return self.is_audio_path(path) or is_music_media_source(media_source)

    def recognize_by_path(
        self,
        path: str,
        media_source: Optional[MediaSource] = None,
        episode_group: Optional[str] = None,
        obtain_images: bool = False,
    ) -> Optional[Context]:
        """
        根据文件路径识别媒体信息，影视与音乐统一入口

        :param path: 文件路径
        :param media_source: 请求级识别数据源
        :param episode_group: 剧集组
        :param obtain_images: 是否补充图片
        :return: 识别上下文
        """
        request = _build_path_recognition_request(
            path=path,
            media_source=media_source,
            episode_group=episode_group,
            obtain_images=obtain_images,
            is_music=self._is_music_path_request(path, media_source),
        )
        logger.info(f"开始识别媒体信息，文件：{request.path} ...")
        if request.route is _PathRoute.MUSIC:
            music_meta, music_info = self.recognize_music_by_path(
                request.path,
                media_source=request.media_source,
            )
            return _path_context(music_meta, music_info)
        file_meta = MetaInfoPath(Path(request.path))
        mediainfo = self._recognize_with_fallback_by_meta(
            metainfo=file_meta,
            media_source=request.media_source,
            episode_group=request.episode_group,
            obtain_images=request.obtain_images,
        )
        if not mediainfo:
            logger.warn(f"{request.path} 未识别到媒体信息")
        return _path_context(file_meta, mediainfo)

    async def async_recognize_by_path(
        self,
        path: str,
        media_source: Optional[MediaSource] = None,
        episode_group: Optional[str] = None,
        obtain_images: bool = False,
    ) -> Optional[Context]:
        """
        根据文件路径识别媒体信息，影视与音乐统一入口（异步版本）

        :param path: 文件路径
        :param media_source: 请求级识别数据源
        :param episode_group: 剧集组
        :param obtain_images: 是否补充图片
        :return: 识别上下文
        """
        request = _build_path_recognition_request(
            path=path,
            media_source=media_source,
            episode_group=episode_group,
            obtain_images=obtain_images,
            is_music=self._is_music_path_request(path, media_source),
        )
        logger.info(f"开始识别媒体信息，文件：{request.path} ...")
        if request.route is _PathRoute.MUSIC:
            music_meta, music_info = await self.async_recognize_music_by_path(
                request.path,
                media_source=request.media_source,
            )
            return _path_context(music_meta, music_info)
        file_meta = MetaInfoPath(Path(request.path))
        mediainfo = await self._async_recognize_with_fallback_by_meta(
            metainfo=file_meta,
            media_source=request.media_source,
            episode_group=request.episode_group,
            obtain_images=request.obtain_images,
        )
        if not mediainfo:
            logger.warn(f"{request.path} 未识别到媒体信息")
        return _path_context(file_meta, mediainfo)
