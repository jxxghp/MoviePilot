"""音频证据、单曲层级与统一路径识别 owner。"""

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Generator, Optional, Tuple, TypeGuard, Union, cast

from app.application.audio import AudioMetadataHelper
from app.application.configuration import get_chain_runtime_config_snapshot
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
    return identity_meta


def _without_music_identity(meta: MetaMusic) -> MetaMusic:
    """复制音乐元数据并移除可能误导标题搜索的远程身份。"""
    clean_meta = MetaMusic.from_dict(meta.to_dict())
    clean_meta.media_source = None
    clean_meta.media_id = None
    return clean_meta


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
    return info


def _finalize_music_path_info(
    meta: MetaMusic,
    info: Optional[MusicInfo],
) -> MusicInfo:
    """统一远端命中和本地兜底的音频质量合并。"""
    result = _merge_music_audio_quality(info or MusicInfo.from_meta(meta), meta)
    if (
        _has_remote_music_identity(result)
        and result.music_type == MUSIC_ENTITY_RECORDING
        and not result.album_type
    ):
        # MusicBrainz 的部分独立 Recording 能精确命中，但关联 Release
        # Group 没有 primary-type。路径识别必须在分类服务运行前补齐类型，
        # 才能由统一规则稳定归入 Single，而不是落到“未分类”。本地标签兜底
        # 没有远程身份，不参与该推断。
        result.album_type = "Single"
    return result


def _fingerprint_info_matches_evidence(
    info: Optional[MusicInfo],
    tag_meta: Optional[MetaMusic],
    filename_meta: Optional[MetaMusic],
) -> bool:
    """Require an AcoustID candidate to agree with local textual evidence.

    Public AcoustID mappings can point to the wrong MusicBrainz recording even
    at a high score.  A hit is therefore only authoritative when its title and
    version, plus any locally available artist credit, agree with the tags or
    parsed filename.
    """
    from difflib import SequenceMatcher
    from app.domain.music import (  # pylint: disable=import-outside-toplevel
        music_album_matches,
        music_artist_matches,
        music_base_title,
        music_text_key,
        music_title_matches,
        music_titles,
        music_version_matches,
        music_year_matches,
    )

    if not _has_remote_music_identity(info):
        return False
    primary = tag_meta if tag_meta and tag_meta.title else filename_meta
    if not primary or not primary.title:
        return False
    artist_evidence = primary.artists or (
        filename_meta.artists if filename_meta else []
    )
    # 只有曲名时，公开 AcoustID 映射中的同名录音无法排除。
    # 必须由文件标签、文件名或同目录共识提供艺人证据。
    if not artist_evidence:
        return False
    if not music_artist_matches(info, artist_evidence):
        return False
    if not music_version_matches(info, primary):
        return False
    if (
        primary.album
        and info.album
        and not music_album_matches(info, primary.album)
        and not _is_standalone_single_evidence(primary)
    ):
        return False
    if not _is_standalone_single_evidence(primary) and not music_year_matches(
        info,
        primary,
    ):
        return False
    if music_title_matches(info, primary.title):
        return True
    # AcoustID is strong audio evidence once the artist agrees.  Allow common
    # radio/edit/remaster suffixes and very small legacy-tag typos, while still
    # rejecting unrelated recordings.
    evidence_key = music_text_key(music_base_title(primary.title))
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
            music_base_title(_FINGERPRINT_TITLE_QUALIFIER.sub("", primary.title))
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
) -> MusicInfo:
    """用明确的本地单曲标签校正指纹录音所选中的任意关联发行版。

    MusicBrainz Recording 可以同时收录于单曲和原声专辑。指纹证明的是录音
    身份，而不是具体发行版；当本地标题和专辑同名时，保留录音 MBID，并把
    发行层字段收敛为本地单曲证据，避免错误归入远端返回的另一张专辑。
    """
    from app.domain.music import (  # pylint: disable=import-outside-toplevel
        music_album_matches,
        music_year_matches,
    )

    primary = tag_meta if tag_meta and tag_meta.title else filename_meta
    if (
        not primary
        or not _is_standalone_single_evidence(primary)
        or not primary.album
        or not info.album
        or (
            music_album_matches(info, primary.album)
            and music_year_matches(info, primary)
        )
    ):
        return info

    reconciled = MusicInfo.from_dict(info.to_dict())
    reconciled.album = primary.album
    reconciled.album_artist = primary.album_artist or info.artist
    reconciled.album_id = None
    reconciled.album_type = "Single"
    reconciled.secondary_types = []
    reconciled.year = int(primary.year) if primary.year is not None else None
    reconciled.release_date = None
    reconciled.release_status = None
    reconciled.disc_number = primary.disc_number
    reconciled.track_number = primary.track_number
    reconciled.total_tracks = primary.total_tracks
    reconciled.cover_url = None
    reconciled.category = ""
    reconciled.metadata_category = "Single"
    reconciled.classification = None
    return reconciled


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
        search_meta = _without_music_identity(meta)
    if not search_meta.title:
        return _MusicTierOutcome()
    result = yield _MusicTierAction(
        kind=_MusicTierActionKind.SEARCH,
        meta=search_meta,
    )
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
        if _has_remote_music_identity(info):
            return info
    info = yield _MusicPathAction(
        kind=_MusicPathActionKind.TAG,
        meta=tag_meta,
        tier_name="文件标签",
    )
    if _has_remote_music_identity(info):
        return info
    info = yield _MusicPathAction(
        kind=_MusicPathActionKind.FILENAME,
        meta=filename_meta,
        tier_name="文件名",
    )
    if _has_remote_music_identity(info):
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
    def read_path_meta(cls, path: Union[str, Path]) -> MetaMusic:
        """读取本地音频标签，不可访问时回退到文件名和目录线索。"""
        file_path = Path(path)
        if file_path.exists() and file_path.is_file():
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
        return matched.get(str(file_path.resolve()))

    def recognize_music_by_path(
        self,
        path: Union[str, Path],
        media_source: Optional[MediaSource] = None,
        contextual_meta: Optional[MetaMusic] = None,
    ) -> Tuple[MetaMusic, MusicInfo]:
        """按指纹、文件标签、文件名三级顺序识别本地音乐。"""
        meta, tag_meta, filename_meta = AudioMetadataHelper.read_evidence(Path(path))
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
                    recording_id = AcoustIdChain().identify_music_by_fingerprint(path)
                    info = self._recognize_musicbrainz_recording(meta, recording_id) if recording_id else None
                    if self._is_remote_music_info(info) and not _fingerprint_info_matches_evidence(
                        info, tag_meta, filename_meta,
                    ):
                        logger.warning(
                            "AcoustID 候选与本地标签/文件名不符，"
                            f"已回退文本识别：{Path(path).name} -> {info.artist} - {info.title}"
                        )
                        info = None
                    if self._is_remote_music_info(info):
                        info = _reconcile_fingerprint_release(
                            info,
                            tag_meta,
                            filename_meta,
                        )
                        logger.info("音乐识别命中 AcoustID 指纹层，已跳过标签和文件名识别")
                elif action.kind is _MusicPathActionKind.ALBUM:
                    info = self._music_album_dir_fallback(path)
                    if info and not _music_info_matches_text_evidence(info, meta):
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
                    recording_id = await AcoustIdChain().async_identify_music_by_fingerprint(path)
                    info = (
                        await self._async_recognize_musicbrainz_recording(
                            meta,
                            recording_id,
                        )
                        if recording_id
                        else None
                    )
                    if self._is_remote_music_info(info) and not _fingerprint_info_matches_evidence(
                        info, tag_meta, filename_meta,
                    ):
                        logger.warning(
                            "AcoustID 候选与本地标签/文件名不符，"
                            f"已回退文本识别：{Path(path).name} -> {info.artist} - {info.title}"
                        )
                        info = None
                    if self._is_remote_music_info(info):
                        info = _reconcile_fingerprint_release(
                            info,
                            tag_meta,
                            filename_meta,
                        )
                        logger.info("音乐识别命中 AcoustID 指纹层，已跳过标签和文件名识别")
                elif action.kind is _MusicPathActionKind.ALBUM:
                    info = await self._async_music_album_dir_fallback(path)
                    if info and not _music_info_matches_text_evidence(info, meta):
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
