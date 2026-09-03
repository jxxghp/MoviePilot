"""专辑目录扫描、曲目对齐与缓存编排 owner。"""

import os
from pathlib import Path
from typing import Optional, Union, cast

from app.application.audio import AudioMetadataHelper
from app.application.configuration import get_chain_runtime_config_snapshot
from app.chain.media.cache import AlbumSignature
from app.chain.media.contract import _MediaOwnerBase
from app.chain.musicbrainz import MusicBrainzChain
from app.domain.context import (
    MusicInfo,
)
from app.domain.meta.metamusic import MetaMusic
from app.foundation.text import convert as zhconv_convert
from app.runtime.execution import run_in_threadpool


def _is_directory(path: Path) -> bool:
    """判断路径是否仍指向目录。"""
    return path.is_dir()


def _album_directory_cache_key(directory: Path) -> str:
    """返回保留符号链接别名的绝对目录键，避免不同目录语义错误共享结果。"""
    return os.path.abspath(directory)


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

    def _match_music_album_directory(
        self,
        directory: Path,
        files: list[Path],
    ) -> dict[str, MusicInfo]:
        """同步汇总本地专辑证据并委托 MusicBrainz 来源链匹配。"""
        metas = AudioMetadataHelper.read_many(files)
        album_meta = MetaMusic.from_album_context(directory.name, metas)
        album = MusicBrainzChain().match_music_album(album_meta, metas)
        if not album or not album.tracks:
            return {}
        return {
            str(file.resolve()): info
            for file, info in self._align_music_album_tracks(files, metas, album.tracks).items()
        }

    async def _async_match_music_album_directory(
        self,
        directory: Path,
        files: list[Path],
    ) -> dict[str, MusicInfo]:
        """异步汇总本地专辑证据并委托 MusicBrainz 来源链匹配。"""
        metas = await run_in_threadpool(AudioMetadataHelper.read_many, files)
        album_meta = MetaMusic.from_album_context(directory.name, metas)
        album = await MusicBrainzChain().async_match_music_album(album_meta, metas)
        if not album or not album.tracks:
            return {}
        return {
            str(file.resolve()): info
            for file, info in self._align_music_album_tracks(files, metas, album.tracks).items()
        }

    def recognize_music_album_directory(
        self,
        path: Union[str, Path],
    ) -> dict[str, MusicInfo]:
        """按目录级线索批量识别整张专辑并返回文件到曲目的映射。"""
        directory = Path(path)
        if not directory.is_dir():
            return {}
        files = self._directory_audio_files(directory)
        if len(files) < self._album_match_min_files:
            return {}
        key = _album_directory_cache_key(directory)
        signature = self._album_directory_signature(directory, files)
        matched = self._album_dir_cache.resolve(
            key,
            signature,
            lambda: self._match_music_album_directory(directory, files),
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
    ) -> dict[str, MusicInfo]:
        """异步按目录级线索批量识别整张专辑。"""
        directory = Path(path)
        if not await run_in_threadpool(_is_directory, directory):
            return {}
        files = await run_in_threadpool(self._directory_audio_files, directory)
        if len(files) < self._album_match_min_files:
            return {}
        key = _album_directory_cache_key(directory)
        signature = self._album_directory_signature(directory, files)
        matched = await self._album_dir_cache.async_resolve(
            key,
            signature,
            lambda: self._async_match_music_album_directory(directory, files),
        )
        simplified = self._simplify_recognized_music_mapping(matched)
        return {
            item_path: cast(
                MusicInfo,
                await self._async_finalize_recognition_result(info),
            )
            for item_path, info in simplified.items()
        }
