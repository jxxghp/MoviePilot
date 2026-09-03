"""音频证据、单曲层级与统一路径识别 owner。"""

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
    return _merge_music_audio_quality(info or MusicInfo.from_meta(meta), meta)


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
    def _is_remote_music_info(info: Optional[MusicInfo]) -> bool:
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
    ) -> Tuple[MetaMusic, MusicInfo]:
        """按指纹、文件标签、文件名三级顺序识别本地音乐。"""
        meta, tag_meta, filename_meta = AudioMetadataHelper.read_evidence(Path(path))
        plan = _music_path_plan(tag_meta, filename_meta, media_source)
        info: Optional[MusicInfo] = None
        try:
            action = next(plan)
            while True:
                if action.kind is _MusicPathActionKind.FINGERPRINT:
                    recording_id = AcoustIdChain().identify_music_by_fingerprint(path)
                    info = self._recognize_musicbrainz_recording(meta, recording_id) if recording_id else None
                    if self._is_remote_music_info(info):
                        logger.info("音乐识别命中 AcoustID 指纹层，已跳过标签和文件名识别")
                elif action.kind is _MusicPathActionKind.ALBUM:
                    info = self._music_album_dir_fallback(path)
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
    ) -> Tuple[MetaMusic, MusicInfo]:
        """异步按指纹、文件标签、文件名三级顺序识别本地音乐。"""
        meta, tag_meta, filename_meta = await run_in_threadpool(
            AudioMetadataHelper.read_evidence,
            Path(path),
        )
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
                    if self._is_remote_music_info(info):
                        logger.info("音乐识别命中 AcoustID 指纹层，已跳过标签和文件名识别")
                elif action.kind is _MusicPathActionKind.ALBUM:
                    info = await self._async_music_album_dir_fallback(path)
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
