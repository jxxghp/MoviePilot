"""专辑目录扫描、曲目对齐与缓存编排 owner。"""

import os
from copy import deepcopy
from pathlib import Path
from typing import Optional, Union, cast

from app.application.audio import AudioMetadataHelper
from app.application.configuration import get_chain_runtime_config_snapshot
from app.chain.media.cache import AlbumSignature
from app.chain.media.contract import _MediaOwnerBase
from app.chain.musicbrainz import MusicBrainzChain
from app.domain.context import (
    MusicAlbumInfo,
    MusicInfo,
)
from app.domain.meta.metamusic import MetaMusic
from app.foundation.text import convert as zhconv_convert
from app.runtime.execution import run_in_threadpool


def _is_directory(path: Path) -> bool:
    """判断路径是否仍指向目录。"""
    return path.is_dir()


def _album_directory_cache_key(
    directory: Path,
    regions: tuple[str, ...],
    scripts: tuple[str, ...],
) -> str:
    """返回包含发行偏好的目录键，避免不同手动选择错误共享结果。"""
    return "|".join((os.path.abspath(directory), ",".join(regions), ",".join(scripts)))


class MediaAlbumOwner(_MediaOwnerBase):
    """专辑目录扫描、曲目对齐与缓存编排 owner。"""

    @classmethod
    def _directory_audio_files(cls, directory: Path) -> list[Path]:
        """收集专辑目录及其一级碟片子目录中的音频文件。"""
        files: list[Path] = []

        def collect(current: Path) -> None:
            """收集单层目录中的可见音频文件。"""
            try:
                entries = sorted(current.iterdir())
            except OSError:
                return
            files.extend(
                item
                for item in entries
                if not item.name.startswith(".")
                and item.is_file()
                and item.suffix.lower() in get_chain_runtime_config_snapshot().audio_extensions
            )

        collect(directory)
        try:
            subdirectories = sorted(
                item for item in directory.iterdir() if item.is_dir() and not item.name.startswith(".")
            )
        except OSError:
            subdirectories = []
        for subdirectory in subdirectories:
            if MetaMusic.parse_disc_dir(subdirectory.name) is not None:
                collect(subdirectory)
        return files

    @staticmethod
    def _album_directory_signature(
        directory: Path,
        files: list[Path],
    ) -> AlbumSignature:
        """按相对路径、大小和纳秒修改时间生成目录缓存签名。"""
        signature: list[tuple[str, int, int]] = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                signature.append((str(path.relative_to(directory)).casefold(), -1, -1))
            else:
                signature.append(
                    (
                        str(path.relative_to(directory)).casefold(),
                        stat.st_size,
                        stat.st_mtime_ns,
                    )
                )
        return tuple(signature)

    @staticmethod
    def _music_track_title_key(value: Optional[str]) -> str:
        """统一繁简、大小写和标点，生成专辑曲目精确对位键。"""
        text = str(value or "")
        try:
            text = zhconv_convert(text, "zh-hans")
        except Exception:  # pylint: disable=broad-except
            pass
        return str(MetaMusic.compact_text(text))

    @classmethod
    def _align_music_album_tracks(
        cls,
        files: list[Path],
        metas: list[MetaMusic],
        tracks: list[MusicInfo],
    ) -> dict[Path, MusicInfo]:
        """优先按精确曲名、再按碟号和曲序把专辑曲目对位到本地文件。"""
        matched: dict[Path, MusicInfo] = {}
        used: set[int] = set()
        indexed_tracks = list(enumerate(tracks))
        by_title: dict[str, list[int]] = {}
        for index, track in indexed_tracks:
            title_key = cls._music_track_title_key(track.title)
            if title_key:
                by_title.setdefault(title_key, []).append(index)

        pending: list[tuple[Path, MetaMusic]] = []
        for file, meta in zip(files, metas):
            candidates = [
                index for index in by_title.get(cls._music_track_title_key(meta.title), []) if index not in used
            ]
            if candidates:
                local_position = (meta.disc_number or 1, meta.track_number or 0)
                index = next(
                    (
                        candidate
                        for candidate in candidates
                        if (
                            tracks[candidate].disc_number or 1,
                            tracks[candidate].track_number or 0,
                        )
                        == local_position
                    ),
                    candidates[0],
                )
                matched[file] = tracks[index]
                used.add(index)
            else:
                pending.append((file, meta))

        by_position: dict[tuple[int, int], list[int]] = {}
        for index, track in indexed_tracks:
            if track.track_number:
                by_position.setdefault((track.disc_number or 1, track.track_number), []).append(index)
        unresolved: list[tuple[Path, MetaMusic]] = []
        for file, meta in pending:
            position_key = (meta.disc_number or 1, meta.track_number or 0)
            candidates = (
                [index for index in by_position.get(position_key, []) if index not in used] if meta.track_number else []
            )
            if candidates:
                index = candidates[0]
                matched[file] = tracks[index]
                used.add(index)
            else:
                unresolved.append((file, meta))
        remaining = [track for index, track in indexed_tracks if index not in used]
        unresolved.sort(key=lambda item: (item[1].disc_number or 1, item[0].name.casefold()))
        for (file, _), track in zip(unresolved, remaining):
            matched[file] = track
        return matched

    @classmethod
    def _align_selected_music_album(
        cls,
        files: list[Path],
        album: MusicAlbumInfo,
    ) -> dict[str, MusicInfo]:
        """读取本地标签，并把手动选择发行版的曲目对齐到文件。"""
        metas = AudioMetadataHelper.read_many(files)
        return {
            str(file.resolve()): info
            for file, info in cls._align_music_album_tracks(
                files,
                metas,
                album.tracks,
            ).items()
        }

    @classmethod
    def _album_track_map(
        cls,
        files: list[Path],
        metas: list[MetaMusic],
        album: MusicAlbumInfo,
    ) -> dict[str, MusicInfo]:
        """将专辑分类与识别事实传递给每条已对位曲目。"""
        aligned = cls._align_music_album_tracks(files, metas, album.tracks)
        for info in aligned.values():
            info.set_library_category(album.library_category)
            info.classification = deepcopy(album.classification)
            info.classification_facts = dict(album.classification_facts)
        return {
            str(file.resolve()): info
            for file, info in aligned.items()
        }

    def _match_music_album_directory(
        self,
        directory: Path,
        files: list[Path],
        music_release_regions: Optional[tuple[str, ...]] = None,
        music_release_scripts: Optional[tuple[str, ...]] = None,
    ) -> dict[str, MusicInfo]:
        """同步汇总本地专辑证据并委托 MusicBrainz 来源链匹配。"""
        metas = AudioMetadataHelper.read_many(files)
        album_meta = MetaMusic.from_album_context(directory.name, metas)
        regions, scripts = self._music_release_preferences(
            list(music_release_regions) if music_release_regions is not None else None,
            list(music_release_scripts) if music_release_scripts is not None else None,
        )
        album = MusicBrainzChain().match_music_album(
            album_meta,
            metas,
            music_release_regions=list(regions),
            music_release_scripts=list(scripts),
        )
        if not album or not album.tracks:
            return {}
        return self._album_track_map(files, metas, album)

    async def _async_match_music_album_directory(
        self,
        directory: Path,
        files: list[Path],
        music_release_regions: Optional[tuple[str, ...]] = None,
        music_release_scripts: Optional[tuple[str, ...]] = None,
    ) -> dict[str, MusicInfo]:
        """异步汇总本地专辑证据并委托 MusicBrainz 来源链匹配。"""
        metas = await run_in_threadpool(AudioMetadataHelper.read_many, files)
        album_meta = MetaMusic.from_album_context(directory.name, metas)
        regions, scripts = self._music_release_preferences(
            list(music_release_regions) if music_release_regions is not None else None,
            list(music_release_scripts) if music_release_scripts is not None else None,
        )
        album = await MusicBrainzChain().async_match_music_album(
            album_meta,
            metas,
            music_release_regions=list(regions),
            music_release_scripts=list(scripts),
        )
        if not album or not album.tracks:
            return {}
        return self._album_track_map(files, metas, album)

    def recognize_music_album_directory(
        self,
        path: Union[str, Path],
        music_release_regions: Optional[list[str]] = None,
        music_release_scripts: Optional[list[str]] = None,
    ) -> dict[str, MusicInfo]:
        """按目录级线索批量识别整张专辑并返回文件到曲目的映射。"""
        directory = Path(path)
        if not directory.is_dir():
            return {}
        files = self._directory_audio_files(directory)
        if len(files) < self._album_match_min_files:
            return {}
        regions, scripts = self._music_release_preferences(
            music_release_regions,
            music_release_scripts,
        )
        custom_preference = music_release_regions is not None or music_release_scripts is not None
        key = _album_directory_cache_key(directory, regions, scripts)
        signature = self._album_directory_signature(directory, files)
        matched = self._album_dir_cache.resolve(
            key,
            signature,
            lambda: (
                self._match_music_album_directory(directory, files, regions, scripts)
                if custom_preference
                else self._match_music_album_directory(directory, files)
            ),
        )
        simplified = self._simplify_recognized_music_mapping(matched)
        finalized: dict[str, MusicInfo] = {}
        for item_path, info in simplified.items():
            finalized[item_path] = cast(
                MusicInfo,
                self._finalize_recognition_result(info),
            )
        return finalized

    async def async_recognize_music_album_directory(
        self,
        path: Union[str, Path],
        music_release_regions: Optional[list[str]] = None,
        music_release_scripts: Optional[list[str]] = None,
    ) -> dict[str, MusicInfo]:
        """异步按目录级线索批量识别整张专辑。"""
        directory = Path(path)
        if not await run_in_threadpool(_is_directory, directory):
            return {}
        files = await run_in_threadpool(self._directory_audio_files, directory)
        if len(files) < self._album_match_min_files:
            return {}
        regions, scripts = self._music_release_preferences(
            music_release_regions,
            music_release_scripts,
        )
        custom_preference = music_release_regions is not None or music_release_scripts is not None
        key = _album_directory_cache_key(directory, regions, scripts)
        signature = self._album_directory_signature(directory, files)
        matched = await self._album_dir_cache.async_resolve(
            key,
            signature,
            lambda: (
                self._async_match_music_album_directory(directory, files, regions, scripts)
                if custom_preference
                else self._async_match_music_album_directory(directory, files)
            ),
        )
        simplified = self._simplify_recognized_music_mapping(matched)
        return {
            item_path: cast(
                MusicInfo,
                await self._async_finalize_recognition_result(info),
            )
            for item_path, info in simplified.items()
        }

    @staticmethod
    def _music_release_preferences(
        regions: Optional[list[str]],
        scripts: Optional[list[str]],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """解析请求级发行偏好；未指定时继承一次稳定的系统配置快照。"""
        runtime = get_chain_runtime_config_snapshot()
        normalized_regions = tuple(regions) if regions is not None else runtime.music_release_region_priority
        normalized_scripts = tuple(scripts) if scripts is not None else runtime.music_release_script_priority
        return normalized_regions, normalized_scripts
