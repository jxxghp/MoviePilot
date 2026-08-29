"""音频证据、单曲层级与统一路径识别 owner。"""

from pathlib import Path
from typing import Optional, Tuple, Union

from app.application.audio import AudioMetadataHelper
from app.application.configuration import get_chain_runtime_config_snapshot
from app.chain.acoustid import AcoustIdChain
from app.chain.media.contract import _MediaOwnerBase
from app.domain.context import (
    Context,
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
        for key in ("audio_format", "audio_lossless", "bit_depth", "sample_rate", "bitrate"):
            value = getattr(meta, key, None)
            if value is not None:
                setattr(info, key, value)
        return info

    @staticmethod
    def _clear_music_identity(meta: MetaMusic) -> MetaMusic:
        """复制音乐元数据并清除远程身份，供直查失败后按要素重新匹配。"""
        clean_meta = MetaMusic.from_dict(meta.to_dict())
        clean_meta.media_source = None
        clean_meta.media_id = None
        return clean_meta

    @staticmethod
    def _is_remote_music_info(info: Optional[MusicInfo]) -> bool:
        """判断音乐识别结果是否携带可复用的远程身份。"""
        return bool(info and info.media_source and info.media_id)

    def _recognize_musicbrainz_recording(
        self,
        meta: MetaMusic,
        recording_id: str,
    ) -> Optional[MusicInfo]:
        """按已知 MusicBrainz Recording ID 直接读取单曲详情。"""
        identity_meta = MetaMusic.from_dict(meta.to_dict())
        identity_meta.media_source = MediaSource.MusicBrainz
        identity_meta.media_id = recording_id
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
        identity_meta = MetaMusic.from_dict(meta.to_dict())
        identity_meta.media_source = MediaSource.MusicBrainz
        identity_meta.media_id = recording_id
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
        if not meta:
            return None
        normalized_source = normalize_media_source(media_source)
        search_meta = meta
        if meta.media_source == MediaSource.MusicBrainz and meta.media_id:
            if normalized_source in (None, MediaSource.MusicBrainz):
                direct = self._recognize_musicbrainz_recording(
                    meta=meta,
                    recording_id=str(meta.media_id),
                )
                if self._is_remote_music_info(direct):
                    logger.info(f"音乐识别命中{tier_name}层 MusicBrainz ID 直查")
                    return direct
            search_meta = self._clear_music_identity(meta)
        if not search_meta.title:
            return None
        result = self.recognize_media(
            meta=search_meta,
            media_source=media_source,
            music_type=MUSIC_ENTITY_RECORDING,
        )
        if isinstance(result, MusicInfo) and self._is_remote_music_info(result):
            logger.info(f"音乐识别命中{tier_name}层：{result.title}")
            return result
        return None

    async def _async_recognize_music_meta_tier(
        self,
        meta: Optional[MetaMusic],
        media_source: Optional[MediaSource],
        tier_name: str,
    ) -> Optional[MusicInfo]:
        """异步识别单个音乐元数据证据层，标签中的 MBID 优先直查。"""
        if not meta:
            return None
        normalized_source = normalize_media_source(media_source)
        search_meta = meta
        if meta.media_source == MediaSource.MusicBrainz and meta.media_id:
            if normalized_source in (None, MediaSource.MusicBrainz):
                direct = await self._async_recognize_musicbrainz_recording(
                    meta=meta,
                    recording_id=str(meta.media_id),
                )
                if self._is_remote_music_info(direct):
                    logger.info(f"音乐识别命中{tier_name}层 MusicBrainz ID 直查")
                    return direct
            search_meta = self._clear_music_identity(meta)
        if not search_meta.title:
            return None
        result = await self.async_recognize_media(
            meta=search_meta,
            media_source=media_source,
            music_type=MUSIC_ENTITY_RECORDING,
        )
        if isinstance(result, MusicInfo) and self._is_remote_music_info(result):
            logger.info(f"音乐识别命中{tier_name}层：{result.title}")
            return result
        return None

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
        info = None
        normalized_source = normalize_media_source(media_source)
        if normalized_source in (None, MediaSource.MusicBrainz):
            recording_id = AcoustIdChain().identify_music_by_fingerprint(path)
            if recording_id:
                info = self._recognize_musicbrainz_recording(meta, recording_id)
                if self._is_remote_music_info(info):
                    logger.info("音乐识别命中 AcoustID 指纹层，已跳过标签和文件名识别")
        if not self._is_remote_music_info(info):
            info = self._recognize_music_meta_tier(
                meta=tag_meta,
                media_source=media_source,
                tier_name="文件标签",
            )
        if not self._is_remote_music_info(info):
            info = self._recognize_music_meta_tier(
                meta=filename_meta,
                media_source=media_source,
                tier_name="文件名",
            )
        result = self._merge_music_audio_quality(info or self._music_info_from_path_meta(meta), meta)
        if not result.media_source and media_source in (None, MediaSource.MusicBrainz):
            # 单曲搜索未命中时，按所在目录做专辑级匹配兑底
            matched = self._music_album_dir_fallback(path)
            if matched:
                result = self._merge_music_audio_quality(matched, meta)
        return meta, self._simplify_recognized_music_info(result)

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
        info = None
        normalized_source = normalize_media_source(media_source)
        if normalized_source in (None, MediaSource.MusicBrainz):
            recording_id = await AcoustIdChain().async_identify_music_by_fingerprint(path)
            if recording_id:
                info = await self._async_recognize_musicbrainz_recording(
                    meta,
                    recording_id,
                )
                if self._is_remote_music_info(info):
                    logger.info("音乐识别命中 AcoustID 指纹层，已跳过标签和文件名识别")
        if not self._is_remote_music_info(info):
            info = await self._async_recognize_music_meta_tier(
                meta=tag_meta,
                media_source=media_source,
                tier_name="文件标签",
            )
        if not self._is_remote_music_info(info):
            info = await self._async_recognize_music_meta_tier(
                meta=filename_meta,
                media_source=media_source,
                tier_name="文件名",
            )
        result = self._merge_music_audio_quality(info or self._music_info_from_path_meta(meta), meta)
        if not result.media_source and media_source in (None, MediaSource.MusicBrainz):
            # 单曲搜索未命中时，按所在目录做专辑级匹配兑底
            matched = await self._async_music_album_dir_fallback(path)
            if matched:
                result = self._merge_music_audio_quality(matched, meta)
        return meta, self._simplify_recognized_music_info(result)

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
        logger.info(f"开始识别媒体信息，文件：{path} ...")
        # 音频文件直接在本链完成标签读取、搜索匹配与专辑目录兜底，封面等图片由刮削环节补充
        if self._is_music_path_request(path, media_source):
            music_meta, music_info = self.recognize_music_by_path(path, media_source=media_source)
            return Context(meta_info=music_meta, media_info=music_info)
        file_path = Path(path)
        # 元数据
        file_meta = MetaInfoPath(file_path)
        mediainfo = self._recognize_with_fallback_by_meta(
            metainfo=file_meta,
            media_source=media_source,
            episode_group=episode_group,
            obtain_images=obtain_images,
        )
        if not mediainfo:
            logger.warn(f"{path} 未识别到媒体信息")
            return Context(meta_info=file_meta)
        # 返回上下文
        return Context(meta_info=file_meta, media_info=mediainfo)

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
        logger.info(f"开始识别媒体信息，文件：{path} ...")
        # 音频文件直接在本链完成标签读取、搜索匹配与专辑目录兜底，封面等图片由刮削环节补充
        if self._is_music_path_request(path, media_source):
            music_meta, music_info = await self.async_recognize_music_by_path(path, media_source=media_source)
            return Context(meta_info=music_meta, media_info=music_info)
        file_path = Path(path)
        # 元数据
        file_meta = MetaInfoPath(file_path)
        mediainfo = await self._async_recognize_with_fallback_by_meta(
            metainfo=file_meta,
            media_source=media_source,
            episode_group=episode_group,
            obtain_images=obtain_images,
        )
        if not mediainfo:
            logger.warn(f"{path} 未识别到媒体信息")
            return Context(meta_info=file_meta)
        # 返回上下文
        return Context(meta_info=file_meta, media_info=mediainfo)
