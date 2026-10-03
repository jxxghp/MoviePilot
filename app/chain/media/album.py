"""专辑目录扫描、曲目对齐与缓存编排 owner。"""

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional, Union, cast

from app.application.audio import AudioMetadataHelper
from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.music.catalog import MusicSourcePort
from app.application.music.observation import capture_music_recognition, report_music_recognition
from app.application.music.recognition import async_recognize_music_sources, recognize_music_sources, unique_music_match
from app.chain.media.cache import AlbumSignature
from app.chain.media.contract import _MediaOwnerBase
from app.chain.musicbrainz import MusicBrainzChain
from app.domain.context import (
    MusicAlbumInfo,
    MusicInfo,
)
from app.domain.media import music_recognition_sources
from app.domain.meta.metamusic import MUSIC_CREDIT_FIELDS, MetaMusic, music_credit_values
from app.domain.music import (
    MusicDirectoryMatch,
    align_music_tracks,
    expand_music_tracks,
    music_album_candidate_matches,
    music_album_has_consistent_tracks,
    music_album_lookup_plan,
    music_album_title_is_weak,
    music_text_key,
)
from app.foundation.text import convert as zhconv_convert
from app.runtime.execution import run_in_threadpool
from app.schemas.types import MUSIC_ENTITY_ALBUM, MUSIC_ENTITY_RECORDING, MediaSource, MusicEntityType


def _source_directory_result(
    owner: _MediaOwnerBase, files: list[Path], metas: list[MetaMusic], album: Optional[MusicAlbumInfo], diagnostic: dict[str, Any],
) -> MusicDirectoryMatch:
    """次级目录身份不代表具体发行；保留本地多碟曲序、当前年份和原始年份。"""
    if album is None:
        return MusicDirectoryMatch(recognition=diagnostic)
    result = owner._album_track_map(files, metas, album)
    if len(result) != len(files):
        return MusicDirectoryMatch(recognition={**diagnostic, "status": "conflict", "message": "候选专辑不能完整对位当前文件"})
    diagnostic = {**diagnostic, "identity_type": "album", "release_verified": bool(
        album.media_source == MediaSource.MusicBrainz and album.musicbrainz_release_id)}
    for path, meta in zip(files, metas):
        info = result[str(path.resolve())]
        if album.media_source != MediaSource.MusicBrainz:
            for key in ("disc_number", "track_number", "total_tracks", "total_discs", "year", "release_year", "original_year"):
                value = getattr(meta, key, None)
                if value:
                    setattr(info, key, value)
                    if key in meta.field_sources:
                        info.field_sources[key] = meta.field_sources[key]
                    else:
                        info.field_sources.pop(key, None)
        info.raw_data["recognition"] = deepcopy(diagnostic)
    return MusicDirectoryMatch(result, recognition=diagnostic)


def _directory_sources(meta: Optional[MetaMusic]) -> tuple[MediaSource, ...]:
    """发行MBID固定其来源，未绑定时使用当前配置的有序音乐来源。"""
    if meta and any((meta.musicbrainz_release_id, meta.musicbrainz_release_group_id, meta.musicbrainz_release_track_id)):
        return (MediaSource.MusicBrainz,)
    return music_recognition_sources(get_chain_runtime_config_snapshot().search_source)


def _verified_source_album(albums: list[MusicAlbumInfo], meta: MetaMusic, tracks: list[MetaMusic]) -> Optional[MusicAlbumInfo]:
    """只使用完整对位的唯一专辑，返回副本以免污染来源自己的详情缓存。"""
    matched = unique_music_match([album for album in albums if music_album_candidate_matches(album, meta, tracks)])
    if matched is None:
        return None
    result = deepcopy(matched)
    result.raw_data.update(match_coverage=1, match_basis="source_track_evidence")
    return result


def _lookup_source_album(
        chain: MusicSourcePort, source: MediaSource, meta: MetaMusic, tracks: list[MetaMusic],
) -> Optional[MusicAlbumInfo]:
    """通过来源链驱动纯检索计划，豆瓣不将专辑搜索结果伪装成独立录音。"""
    plan = music_album_lookup_plan(source, meta, tracks)
    response: list[MusicInfo] | MusicAlbumInfo | None
    kinds: tuple[MusicEntityType, ...]
    try:
        action = next(plan)
        while True:
            if action.truncated:
                report_music_recognition("ambiguous", "符合名称的候选专辑过多，请补充信息或手动选择")
                return None
            if action.meta is not None:
                kinds = ("album",) if action.meta.music_type == MUSIC_ENTITY_ALBUM else ("recording",)
                response = [] if source == MediaSource.DoubanMusic and action.meta.music_type == MUSIC_ENTITY_RECORDING else (
                    chain.search_music(action.meta, limit=10, music_types=kinds))
            else:
                response = chain.get_music_album(action.album_id or "")
            action = plan.send(response)
    except StopIteration as completed:
        return _verified_source_album(completed.value, meta, tracks)
    finally:
        plan.close()


async def _async_lookup_source_album(
        chain: MusicSourcePort, source: MediaSource, meta: MetaMusic, tracks: list[MetaMusic],
) -> Optional[MusicAlbumInfo]:
    """异步来源采用同一检索计划及候选核验，保持请求顺序和选择结果一致。"""
    plan = music_album_lookup_plan(source, meta, tracks)
    response: list[MusicInfo] | MusicAlbumInfo | None
    kinds: tuple[MusicEntityType, ...]
    try:
        action = next(plan)
        while True:
            if action.truncated:
                report_music_recognition("ambiguous", "符合名称的候选专辑过多，请补充信息或手动选择")
                return None
            if action.meta is not None:
                kinds = ("album",) if action.meta.music_type == MUSIC_ENTITY_ALBUM else ("recording",)
                response = [] if source == MediaSource.DoubanMusic and action.meta.music_type == MUSIC_ENTITY_RECORDING else (
                    await chain.async_search_music(action.meta, limit=10, music_types=kinds))
            else:
                response = await chain.async_get_music_album(action.album_id or "")
            action = plan.send(response)
    except StopIteration as completed:
        return _verified_source_album(completed.value, meta, tracks)
    finally:
        plan.close()


def _album_directory_cache_key(
    directory: Path,
    regions: tuple[str, ...],
    scripts: tuple[str, ...],
    contextual_meta: Optional[MetaMusic] = None,
    file_scope: Optional[list[str]] = None,
    music_sources: Optional[tuple[MediaSource, ...]] = None,
) -> str:
    """将发行偏好及资源证据纳入缓存键，同目录更换种子线索时重新识别。"""
    evidence = {
        key: getattr(contextual_meta, key, None)
        for key in (*MUSIC_CREDIT_FIELDS, "album", "artists", "album_artist", "year", "version", "musicbrainz_release_id",
                    "musicbrainz_release_group_id", "original_year", "release_year")
    } if contextual_meta else None
    if evidence and evidence["album_artist"]:
        evidence["artists"] = [evidence["album_artist"]]
    if evidence and not any(evidence.values()):
        evidence = None
    if evidence and contextual_meta:
        evidence["weak_album"] = not contextual_meta.album or music_album_title_is_weak(contextual_meta)
    sources = [source.value for source in music_sources or _directory_sources(contextual_meta)]
    return json.dumps([os.path.abspath(directory), regions, scripts, evidence, file_scope, sources], ensure_ascii=False, sort_keys=True)


def _apply_album_resource_credits(album_meta: MetaMusic, contextual_meta: MetaMusic) -> None:
    """只复制明确绑定当前发行的角色，不聚合不同曲目的表演阵容或制造来源。"""
    for key, value in music_credit_values(contextual_meta).items():
        if value:
            setattr(album_meta, key, value)
            if key in contextual_meta.field_sources:
                album_meta.field_sources[key] = contextual_meta.field_sources[key]


def _album_context_with_resource(
    directory: Path,
    metas: list[MetaMusic],
    contextual_meta: Optional[MetaMusic],
) -> MetaMusic:
    """以文件自身专辑线索为先，用绑定到本次文件的资源证据补足目录查询。"""
    album_meta = MetaMusic.from_album_context(directory.name, metas)
    for key in ("album", "album_artist"):
        tagged = {
            music_text_key(str(getattr(meta, key))) for meta in metas
            if getattr(meta, key) and meta.field_sources.get(key) in {"tag", "cue", "manual", "album_tags"}
        }
        if len(tagged) > 1:
            album_meta.organization_error = "同一发行范围内的专辑标签冲突，请分别选择发行版本"
        elif tagged:
            album_meta.field_sources[key] = "album_tags"
            if len(metas) == 1:
                setattr(album_meta, key, getattr(metas[0], key))
                if key == "album":
                    album_meta.title = metas[0].album or album_meta.title
                elif key == "album_artist":
                    album_meta.artists = [str(metas[0].album_artist)]
    for key in ("musicbrainz_release_id", "musicbrainz_release_group_id", "release_year", "original_year"):
        values = {getattr(meta, key) for meta in metas if getattr(meta, key)}
        if len(values) > 1 and key != "original_year":
            album_meta.organization_error = "同一发行范围内的发行身份或年份冲突，请核对标签"
        elif len(values) == 1:
            setattr(album_meta, key, next(iter(values)))
            album_meta.field_sources[key] = "album_tags"
        elif contextual_meta:
            setattr(album_meta, key, getattr(contextual_meta, key))
            if key in contextual_meta.field_sources:
                album_meta.field_sources[key] = contextual_meta.field_sources[key]
    if not contextual_meta:
        return album_meta
    _apply_album_resource_credits(album_meta, contextual_meta)
    if contextual_meta.album and not any(meta.album for meta in metas):
        album_meta.album = contextual_meta.album
        album_meta.title = contextual_meta.album
        album_meta.field_sources["album"] = contextual_meta.field_sources.get("album", "torrent")
    if not album_meta.artists:
        album_meta.artists = (
            [contextual_meta.album_artist] if contextual_meta.album_artist
            else list(contextual_meta.artists)
        )
    if not album_meta.album_artist and contextual_meta.album_artist:
        album_meta.album_artist = contextual_meta.album_artist
    for key in ("year", "version"):
        local_values = {getattr(meta, key) for meta in metas if getattr(meta, key)}
        if not getattr(album_meta, key):
            if len(local_values) == 1:
                setattr(album_meta, key, next(iter(local_values)))
            elif not local_values:
                setattr(album_meta, key, getattr(contextual_meta, key))
    return album_meta


class MediaAlbumOwner(_MediaOwnerBase):
    """专辑目录扫描、曲目对齐与缓存编排 owner。"""

    @classmethod
    def clear_music_album_cache(cls) -> None:
        """手动刷新音乐识别时清空所有目录状态，并隔离此前仍在进行的查询。"""
        cls._album_dir_cache.clear()

    @classmethod
    def _directory_audio_files(cls, directory: Path, file_paths: Optional[list[Path]] = None) -> list[Path]:
        """收集专辑目录及其一级碟片子目录中的音频文件。"""
        if file_paths is not None:
            root = directory.absolute()
            return sorted({
                path for raw_path in file_paths if ".." not in raw_path.parts
                for path in (raw_path.absolute(),) if path.is_relative_to(root)
                and path.is_file() and path.suffix.lower() in get_chain_runtime_config_snapshot().audio_extensions
            })
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
        """把音频及同目录 CUE 的路径、大小和修改时间纳入缓存签名。"""
        signature: list[tuple[str, int, int]] = []
        paths = set(files)
        for parent in {path.parent for path in files}:
            try:
                paths.update(sorted(path for path in parent.iterdir()
                                    if not path.name.startswith(".") and path.suffix.casefold() == ".cue" and path.is_file())[:32])
            except OSError:
                continue
        for path in sorted(paths):
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

    def _directory_audio_scan(self, directory: Path, file_paths: Optional[list[Path]] = None) -> tuple[list[Path], AlbumSignature]:
        """在同一工作线程收集文件和CUE签名，避免异步入口直接执行目录和stat I/O。"""
        if not directory.is_dir():
            return [], ()
        files = self._directory_audio_files(directory, file_paths) if file_paths is not None else self._directory_audio_files(directory)
        return files, self._album_directory_signature(directory, files)

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
        *,
        allow_title_override: bool = False,
    ) -> dict[Path, MusicInfo]:
        """只输出有唯一曲目证据的文件，不对剩余文件进行排序猜测。"""
        if len(files) != len(metas):
            return {}
        return {
            files[index]: tracks[remote]
            for index, remote in align_music_tracks(metas, tracks, allow_title_override=allow_title_override).items()
        }

    @classmethod
    def _align_selected_music_album(
        cls,
        files: list[Path],
        album: MusicAlbumInfo,
    ) -> dict[str, MusicInfo]:
        """读取本地标签，并把手动选择发行版的曲目对齐到文件。"""
        return cls._album_track_map(files, AudioMetadataHelper.read_many(files), album, allow_title_override=True)

    @classmethod
    def _album_track_map(
        cls,
        files: list[Path],
        metas: list[MetaMusic],
        album: MusicAlbumInfo,
        *,
        allow_title_override: bool = False,
    ) -> dict[str, MusicInfo]:
        """逻辑曲目全部对位后才输出对应物理文件，整轨始终保持 Album 身份。"""
        if len(files) != len(metas) or not music_album_has_consistent_tracks(album):
            return {}
        logical, owners = expand_music_tracks(metas)
        aligned = align_music_tracks(logical, album.tracks, allow_title_override=allow_title_override)
        result: dict[str, MusicInfo] = {}
        for index, path in enumerate(files):
            positions = [position for position, owner in enumerate(owners) if owner == index]
            if not positions or any(position not in aligned for position in positions):
                continue
            info = album.to_music_info() if metas[index].music_layout == "image_cue" else deepcopy(album.tracks[aligned[positions[0]]])
            if not allow_title_override:
                for key, value in music_credit_values(metas[index]).items():
                    if value:
                        setattr(info, key, value)
                        info.field_sources.pop(key, None)
                        if key in metas[index].field_sources:
                            info.field_sources[key] = metas[index].field_sources[key]
            for key in ("match_score", "match_coverage", "match_basis"):
                if key in album.raw_data:
                    info.raw_data[key] = album.raw_data[key]
            info.set_library_category(album.library_category)
            info.classification = deepcopy(album.classification)
            info.classification_facts = dict(album.classification_facts)
            result[str(path.resolve())] = info
        return result

    def _match_music_album_directory(
        self,
        directory: Path,
        files: list[Path],
        music_release_regions: Optional[tuple[str, ...]] = None,
        music_release_scripts: Optional[tuple[str, ...]] = None,
        contextual_meta: Optional[MetaMusic] = None,
        music_sources: Optional[tuple[MediaSource, ...]] = None,
    ) -> dict[str, MusicInfo]:
        """汇总本地发行证据，已配置来源只有完整对位后才能接管整专整理。"""
        metas = AudioMetadataHelper.read_many(files)
        album_meta = _album_context_with_resource(directory, metas, contextual_meta)
        logical, _owners = expand_music_tracks(metas)
        if album_meta.organization_error:
            return MusicDirectoryMatch(recognition={"status": "conflict", "message": album_meta.organization_error})
        if not logical or (
            len(logical) < self._album_match_min_files
            and not (album_meta.musicbrainz_release_id or album_meta.musicbrainz_release_group_id)
        ):
            return {}
        regions, scripts = self._music_release_preferences(
            list(music_release_regions) if music_release_regions is not None else None,
            list(music_release_scripts) if music_release_scripts is not None else None,
        )
        sources = (MediaSource.MusicBrainz,) if any((album_meta.musicbrainz_release_id, album_meta.musicbrainz_release_group_id)) else (
            music_sources or _directory_sources(album_meta))
        if sources != (MediaSource.MusicBrainz,):
            def recognize(source: MediaSource) -> Optional[MusicAlbumInfo]:
                """MusicBrainz保留具体发行匹配，其它来源必须取得真实曲目表。"""
                if source == MediaSource.MusicBrainz:
                    return MusicBrainzChain().match_music_album(album_meta, logical,
                                                               music_release_regions=list(regions), music_release_scripts=list(scripts))
                chain = self._music_source_chain(source)
                return _lookup_source_album(chain, source, album_meta, logical) if chain else None

            album, diagnostic = recognize_music_sources(sources, recognize, lambda item, source: bool(
                item and item.tracks and item.media_source == source))
            return _source_directory_result(self, files, metas, album, diagnostic)
        with capture_music_recognition() as observation:
            try:
                album = MusicBrainzChain().match_music_album(
                    album_meta, logical, music_release_regions=list(regions), music_release_scripts=list(scripts),
                )
            except Exception:
                return MusicDirectoryMatch(recognition={"status": "service_error", "message": "音乐元数据服务暂时不可用，请稍后重试"})
            if observation.failed or not album or not album.tracks:
                return MusicDirectoryMatch(recognition=observation.to_dict())
            result = self._album_track_map(files, metas, album)
            if len(result) != len(files):
                return MusicDirectoryMatch(recognition={"status": "conflict", "message": "候选专辑无法覆盖当前全部音频，请核对文件范围或发行版本"})
            return MusicDirectoryMatch(result, recognition={**observation.to_dict(), "status": "matched", "message": "专辑及当前曲目已匹配"})

    async def _async_match_music_album_directory(
        self,
        directory: Path,
        files: list[Path],
        music_release_regions: Optional[tuple[str, ...]] = None,
        music_release_scripts: Optional[tuple[str, ...]] = None,
        contextual_meta: Optional[MetaMusic] = None,
        music_sources: Optional[tuple[MediaSource, ...]] = None,
    ) -> dict[str, MusicInfo]:
        """异步整专回退使用相同来源选择、候选范围与完整对位规则。"""
        metas = await run_in_threadpool(AudioMetadataHelper.read_many, files)
        album_meta = _album_context_with_resource(directory, metas, contextual_meta)
        logical, _owners = expand_music_tracks(metas)
        if album_meta.organization_error:
            return MusicDirectoryMatch(recognition={"status": "conflict", "message": album_meta.organization_error})
        if not logical or (
            len(logical) < self._album_match_min_files
            and not (album_meta.musicbrainz_release_id or album_meta.musicbrainz_release_group_id)
        ):
            return {}
        regions, scripts = self._music_release_preferences(
            list(music_release_regions) if music_release_regions is not None else None,
            list(music_release_scripts) if music_release_scripts is not None else None,
        )
        sources = (MediaSource.MusicBrainz,) if any((album_meta.musicbrainz_release_id, album_meta.musicbrainz_release_group_id)) else (
            music_sources or _directory_sources(album_meta))
        if sources != (MediaSource.MusicBrainz,):
            async def recognize(source: MediaSource) -> Optional[MusicAlbumInfo]:
                """通过已有异步来源端口获取发行或专辑，避免同步HTTP进入事件循环。"""
                if source == MediaSource.MusicBrainz:
                    return await MusicBrainzChain().async_match_music_album(album_meta, logical,
                                                                           music_release_regions=list(regions), music_release_scripts=list(scripts))
                chain = self._music_source_chain(source)
                return await _async_lookup_source_album(chain, source, album_meta, logical) if chain else None

            album, diagnostic = await async_recognize_music_sources(sources, recognize, lambda item, source: bool(
                item and item.tracks and item.media_source == source))
            return _source_directory_result(self, files, metas, album, diagnostic)
        with capture_music_recognition() as observation:
            try:
                album = await MusicBrainzChain().async_match_music_album(
                    album_meta, logical, music_release_regions=list(regions), music_release_scripts=list(scripts),
                )
            except Exception:
                return MusicDirectoryMatch(recognition={"status": "service_error", "message": "音乐元数据服务暂时不可用，请稍后重试"})
            if observation.failed or not album or not album.tracks:
                return MusicDirectoryMatch(recognition=observation.to_dict())
            result = self._album_track_map(files, metas, album)
            if len(result) != len(files):
                return MusicDirectoryMatch(recognition={"status": "conflict", "message": "候选专辑无法覆盖当前全部音频，请核对文件范围或发行版本"})
            return MusicDirectoryMatch(result, recognition={**observation.to_dict(), "status": "matched", "message": "专辑及当前曲目已匹配"})

    def recognize_music_album_directory(
        self,
        path: Union[str, Path],
        music_release_regions: Optional[list[str]] = None,
        music_release_scripts: Optional[list[str]] = None,
        contextual_meta: Optional[MetaMusic] = None,
        file_paths: Optional[list[Path]] = None,
    ) -> dict[str, MusicInfo]:
        """按目录级线索批量识别整张专辑并返回文件到曲目的映射。"""
        directory = Path(path).absolute()
        if not directory.is_dir():
            return {}
        files = self._directory_audio_files(directory, file_paths) if file_paths is not None else self._directory_audio_files(directory)
        if not files:
            return {}
        regions, scripts = self._music_release_preferences(
            music_release_regions,
            music_release_scripts,
        )
        scope = [str(file.relative_to(directory)) for file in files] if file_paths is not None else None
        sources = _directory_sources(contextual_meta)
        key = _album_directory_cache_key(directory, regions, scripts, contextual_meta, scope, sources)
        signature = self._album_directory_signature(directory, files)
        matched = self._album_dir_cache.resolve(
            key,
            signature,
            lambda: self._match_music_album_directory(directory, files, regions, scripts, contextual_meta, sources),
        )
        simplified = self._simplify_recognized_music_mapping(matched)
        finalized: dict[str, MusicInfo] = {}
        for item_path, info in simplified.items():
            finalized[item_path] = cast(
                MusicInfo,
                self._finalize_recognition_result(info),
            )
        return MusicDirectoryMatch(finalized, recognition=matched.recognition) if isinstance(matched, MusicDirectoryMatch) else finalized

    async def async_recognize_music_album_directory(
        self,
        path: Union[str, Path],
        music_release_regions: Optional[list[str]] = None,
        music_release_scripts: Optional[list[str]] = None,
        contextual_meta: Optional[MetaMusic] = None,
        file_paths: Optional[list[Path]] = None,
    ) -> dict[str, MusicInfo]:
        """异步按目录级线索批量识别整张专辑。"""
        directory = Path(path).absolute()
        files, signature = await run_in_threadpool(self._directory_audio_scan, directory, file_paths)
        if not files:
            return {}
        regions, scripts = self._music_release_preferences(
            music_release_regions,
            music_release_scripts,
        )
        scope = [str(file.relative_to(directory)) for file in files] if file_paths is not None else None
        sources = _directory_sources(contextual_meta)
        key = _album_directory_cache_key(directory, regions, scripts, contextual_meta, scope, sources)
        matched = await self._album_dir_cache.async_resolve(
            key,
            signature,
            lambda: self._async_match_music_album_directory(directory, files, regions, scripts, contextual_meta, sources),
        )
        simplified = self._simplify_recognized_music_mapping(matched)
        finalized = {
            item_path: cast(
                MusicInfo,
                await self._async_finalize_recognition_result(info),
            )
            for item_path, info in simplified.items()
        }
        return MusicDirectoryMatch(finalized, recognition=matched.recognition) if isinstance(matched, MusicDirectoryMatch) else finalized

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
