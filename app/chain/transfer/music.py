"""音乐整理批次的本地标签准入、目录共识和曲目上下文编排。"""

import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Tuple, Union, cast

from app.application.audio import AudioMetadataHelper
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import music_tags_are_usable, music_text_key
from app.runtime.log import logger
from app.schemas.file import FileItem
from app.schemas.types import MediaType


@dataclass(slots=True)
class MusicBatchContext:
    """同批音乐文件之间的歌词关联与已识别上下文。"""

    related_main_keys: dict[Tuple[str, str], Tuple[str, str]] = field(default_factory=dict)
    single_main_keys: set[Tuple[str, str]] = field(default_factory=set)
    album_main_keys: set[Tuple[str, str]] = field(default_factory=set)
    directory_evidence: dict[Tuple[str, str], MetaMusic] = field(default_factory=dict)
    album_evidence_by_main_key: dict[Tuple[str, str], MetaMusic] = field(default_factory=dict)
    resolved_contexts: dict[Tuple[str, str], tuple[MetaMusic, MusicInfo]] = field(default_factory=dict)


def _music_directory_consensus(values: list[str]) -> Optional[str]:
    """只在同目录至少两个标签高度一致时返回共识值。"""
    normalized = [
        (music_text_key(value), value.strip())
        for value in values
        if value and music_text_key(value)
    ]
    if len(normalized) < 2:
        return None
    counts = Counter(key for key, _value in normalized)
    key, count = counts.most_common(1)[0]
    if count < 2 or count / len(normalized) < 0.8:
        return None
    return next(value for current_key, value in normalized if current_key == key)


def _music_directory_evidence(items: list[FileItem]) -> Optional[MetaMusic]:
    """从同一发行目录的原始音频标签提取可信艺人与专辑共识。"""
    artist_values: list[str] = []
    album_values: list[str] = []
    collective_keys = {
        music_text_key(value)
        for value in ("Various Artists", "Various", "VA", "群星", "众艺人", "眾藝人")
    }
    for item in items:
        if getattr(item, "storage", "local") != "local" or not item.path:
            continue
        tag_meta = AudioMetadataHelper.read_tags(Path(item.path))
        if not tag_meta:
            continue
        artist = tag_meta.album_artist
        if not artist and len(tag_meta.artists) == 1:
            artist = tag_meta.artists[0]
        if artist and music_text_key(artist) not in collective_keys:
            artist_values.append(artist)
        if tag_meta.album:
            album_values.append(tag_meta.album)
    artist = _music_directory_consensus(artist_values)
    album = _music_directory_consensus(album_values)
    if not artist and not album:
        return None
    return MetaMusic(
        artists=[artist] if artist else None,
        album_artist=artist,
        album=album,
    )


def _music_tagged_album_groups(
        items: list[FileItem],
) -> list[tuple[list[FileItem], MetaMusic]]:
    """按精确的专辑艺人和专辑标签划分同目录内的多碟/多发行分组。"""
    grouped: dict[tuple[str, str], tuple[list[FileItem], str, str]] = {}
    collective_keys = {
        music_text_key(value)
        for value in ("Various Artists", "Various", "VA", "群星", "众艺人", "眾藝人")
    }
    for item in items:
        if getattr(item, "storage", "local") != "local" or not item.path:
            continue
        tag_meta = AudioMetadataHelper.read_tags(Path(item.path))
        if not tag_meta or not tag_meta.album:
            continue
        artist = tag_meta.album_artist
        if not artist and len(tag_meta.artists) == 1:
            artist = tag_meta.artists[0]
        artist_key = music_text_key(artist)
        album_key = music_text_key(tag_meta.album)
        if not artist or artist_key in collective_keys or not album_key:
            continue
        key = (artist_key, album_key)
        if key not in grouped:
            grouped[key] = ([], artist.strip(), tag_meta.album.strip())
        grouped[key][0].append(item)
    return [
        (
            grouped_items,
            MetaMusic(artists=[artist], album_artist=artist, album=album),
        )
        for grouped_items, artist, album in grouped.values()
        if len(grouped_items) >= 2
    ]


def _apply_music_directory_evidence(
        meta: MetaMusic,
        evidence: Optional[MetaMusic],
) -> MetaMusic:
    """仅补齐缺失的作品身份字段，不覆盖单曲原有标签。"""
    if not evidence:
        return meta
    merged = deepcopy(meta)
    if not merged.artists and evidence.artists:
        merged.artists = list(evidence.artists)
    if not merged.album_artist and evidence.album_artist:
        merged.album_artist = evidence.album_artist
    if not merged.album and evidence.album:
        merged.album = evidence.album
    return merged


def _apply_music_directory_year(meta: MetaMusic, file_path: Path) -> MetaMusic:
    """用最近发行目录开头的四位年份纠正合集内不可靠的音频年份标签。"""
    for parent in list(file_path.parents)[:4]:
        match = re.match(r"^((?:19|20)\d{2})(?:\D|$)", parent.name)
        if not match:
            continue
        merged = deepcopy(meta)
        merged.year = cast(Any, int(match.group(1)))
        return merged
    return meta


def _local_music_context(
        owner: _TransferOwnerBase,
        file_item: FileItem,
        file_path: Path,
) -> Optional[tuple[MetaMusic, MusicInfo]]:
    """仅以本地真实标签建立快速整理上下文，不向在线来源确认身份。"""
    if file_item.storage != "local" or not owner._is_audio_file(file_item):
        return None
    tags = AudioMetadataHelper.read_tags(file_path)
    if tags is None or not music_tags_are_usable(tags):
        return None
    meta = tags.apply_path_context(file_path)
    logger.info(f"{file_path.name} 使用完整音频标签整理：{meta.artist} - {meta.title}")
    return meta, MusicInfo.from_meta(meta)


def prepare_music_batch_context(
        owner: _TransferOwnerBase,
        file_items: list[tuple[FileItem, bool]],
        batch_mtype: Optional[MediaType],
) -> MusicBatchContext:
    """建立同目录音轨、歌词和单音轨目录的批次索引。"""
    context = MusicBatchContext()
    if batch_mtype != MediaType.MUSIC:
        return context
    main_items_by_dir: dict[Tuple[str, str], list[FileItem]] = {}
    for current_item, current_bluray_dir in file_items:
        if not current_bluray_dir and owner._is_media_file(current_item, MediaType.MUSIC):
            main_items_by_dir.setdefault(
                owner._get_file_parent_key(current_item), []
            ).append(current_item)
    context.single_main_keys = {
        owner._get_file_key(items[0])
        for items in main_items_by_dir.values()
        if len(items) == 1
    }
    for parent_key, items in main_items_by_dir.items():
        evidence = _music_directory_evidence(items)
        if evidence:
            context.directory_evidence[parent_key] = evidence
        # 两首及以上音轨，且目录内艺人和专辑标签均达到高一致性时，足以在
        # 远端临时不可用时证明这是一个专辑目录；不能把整个艺术家合集根目录
        # 当成一张专辑，也不能仅凭文件夹名称猜测类别。
        if evidence and len(items) > 1 and evidence.artists and evidence.album:
            context.album_main_keys.update(
                owner._get_file_key(item) for item in items
            )
        for grouped_items, grouped_evidence in _music_tagged_album_groups(items):
            for item in grouped_items:
                item_key = owner._get_file_key(item)
                context.album_main_keys.add(item_key)
                context.album_evidence_by_main_key[item_key] = grouped_evidence
    for current_item, _current_bluray_dir in file_items:
        if not owner._is_music_lyrics_file(current_item):
            continue
        related_key = owner._get_related_main_file_key(
            current_item,
            main_items_by_dir.get(owner._get_file_parent_key(current_item), []),
        )
        if related_key:
            context.related_main_keys[owner._get_file_key(current_item)] = related_key
    return context


def resolve_music_batch_file_context(
        owner: _TransferOwnerBase,
        *,
        batch_context: MusicBatchContext,
        file_item: FileItem,
        file_path: Path,
        file_meta: Any,
        selected_tracks: dict[str, MusicInfo],
        fallback: Optional[Union[MediaInfo, MusicInfo]],
        discard_shared_identity: bool,
        multi_track_batch: bool,
        release_regions: Optional[list[str]],
        release_scripts: Optional[list[str]],
) -> tuple[Any, Optional[Union[MediaInfo, MusicInfo]]]:
    """解析一项音乐上下文，并让同名歌词复用主音轨结果。"""
    file_key = owner._get_file_key(file_item)
    related_key = batch_context.related_main_keys.get(file_key)
    related_context = (
        batch_context.resolved_contexts.get(related_key)
        if related_key
        else None
    )
    if related_context:
        return deepcopy(related_context[0]), deepcopy(related_context[1])

    file_meta, task_mediainfo = owner._selected_music_task_context(
        file_item,
        file_path,
        file_meta,
        selected_tracks,
        fallback,
    )
    local_context = (
        _local_music_context(owner, file_item, file_path)
        if not task_mediainfo and isinstance(file_meta, MetaMusic)
        else None
    )
    if local_context:
        file_meta, task_mediainfo = local_context
    if isinstance(file_meta, MetaMusic):
        file_key = owner._get_file_key(file_item)
        file_meta = _apply_music_directory_evidence(
            file_meta,
            batch_context.album_evidence_by_main_key.get(file_key)
            or batch_context.directory_evidence.get(owner._get_file_parent_key(file_item)),
        )
        if discard_shared_identity and not local_context:
            file_meta = _apply_music_directory_year(file_meta, file_path)
    if not task_mediainfo and isinstance(file_meta, MetaMusic):
        file_meta, task_mediainfo = _recognize_music_batch_file(
            owner,
            batch_context=batch_context,
            file_item=file_item,
            file_path=file_path,
            file_meta=file_meta,
            discard_shared_identity=discard_shared_identity,
            multi_track_batch=multi_track_batch,
            release_regions=release_regions,
            release_scripts=release_scripts,
        )
    if discard_shared_identity and not local_context and isinstance(file_meta, MetaMusic):
        # 目录年份是当前下载包/发行目录的本地强证据。目录级或逐曲远端识别
        # 可能再次用再版年份覆盖前面已经纠正的年份，因此在所有识别层结束后
        # 重放一次目录年份约束。显式指定 MusicBrainz 身份时不会进入此分支。
        file_meta = _apply_music_directory_year(file_meta, file_path)
        if isinstance(task_mediainfo, MusicInfo) and file_meta.year:
            task_mediainfo = deepcopy(task_mediainfo)
            task_mediainfo.year = file_meta.year
    directory_evidence = (
        batch_context.album_evidence_by_main_key.get(owner._get_file_key(file_item))
        or batch_context.directory_evidence.get(owner._get_file_parent_key(file_item))
    )
    if (
            owner._is_audio_file(file_item)
            and isinstance(file_meta, MetaMusic)
            and isinstance(task_mediainfo, MusicInfo)
            and owner._get_file_key(file_item) in batch_context.album_main_keys
            and directory_evidence
    ):
        # 同一物理发行目录只能生成一个专辑目录。逐曲远端识别可能因服务
        # 波动而混用正式标题、别名或本地兜底；用已验证的目录标签共识统一
        # 专辑和专辑艺人，但保留每首曲目的远端 ID、曲名、封面等信息。
        file_meta = deepcopy(file_meta)
        task_mediainfo = deepcopy(task_mediainfo)
        if directory_evidence.album:
            file_meta.album = directory_evidence.album
            task_mediainfo.album = directory_evidence.album
        if directory_evidence.album_artist:
            file_meta.album_artist = directory_evidence.album_artist
            task_mediainfo.album_artist = directory_evidence.album_artist
        if file_meta.year:
            task_mediainfo.year = file_meta.year
    if (
            owner._is_audio_file(file_item)
            and isinstance(task_mediainfo, MusicInfo)
            and owner._get_file_key(file_item) in batch_context.album_main_keys
            and str(task_mediainfo.album_type or "").casefold()
            not in {"ep", "broadcast", "other"}
    ):
        # 曲目可能同时作为 Single 单独发行。整理完整多音轨目录时，目录内
        # 高一致性的专辑/专辑艺人标签是当前文件归属的更强证据，不能让某一
        # 首歌的单曲发行身份把同一张专辑拆进 Single 分类。
        task_mediainfo = deepcopy(task_mediainfo)
        task_mediainfo.album_type = "Album"
        if not local_context:
            finalized = owner._finalize_recognition_result(task_mediainfo, refresh=True)
            if isinstance(finalized, MusicInfo):
                task_mediainfo = finalized
            task_mediainfo.set_library_category("Album")
    if local_context:
        task_mediainfo = owner._finalize_recognition_result(task_mediainfo, allow_enrichment=False)
    if (
            owner._is_audio_file(file_item)
            and isinstance(file_meta, MetaMusic)
            and isinstance(task_mediainfo, MusicInfo)
    ):
        batch_context.resolved_contexts[owner._get_file_key(file_item)] = (
            deepcopy(file_meta),
            deepcopy(task_mediainfo),
        )
    return file_meta, task_mediainfo


def _recognize_music_batch_file(
        owner: _TransferOwnerBase,
        *,
        batch_context: MusicBatchContext,
        file_item: FileItem,
        file_path: Path,
        file_meta: MetaMusic,
        discard_shared_identity: bool,
        multi_track_batch: bool,
        release_regions: Optional[list[str]],
        release_scripts: Optional[list[str]],
) -> tuple[MetaMusic, Optional[MusicInfo]]:
    """按专辑、曲目证据和单音轨目录结构依次补齐音乐身份。"""
    file_meta, task_mediainfo = owner._match_music_album_context(
        file_item,
        file_path,
        file_meta,
        release_regions,
        release_scripts,
    )
    if not task_mediainfo and discard_shared_identity and owner._is_audio_file(file_item):
        file_meta, task_mediainfo = owner._match_music_recording_context(
            file_item,
            file_path,
            file_meta,
        )
    if task_mediainfo or not discard_shared_identity:
        return file_meta, task_mediainfo

    task_mediainfo = owner._music_info_from_meta(file_meta)
    if (
            multi_track_batch
            and owner._get_file_key(file_item) in batch_context.album_main_keys
    ):
        task_mediainfo.album_type = "Album"
        finalized = owner._finalize_recognition_result(task_mediainfo)
        if isinstance(finalized, MusicInfo):
            task_mediainfo = finalized
        if not task_mediainfo.library_category:
            task_mediainfo.set_library_category("Album")
        return file_meta, task_mediainfo
    if owner._get_file_key(file_item) not in batch_context.single_main_keys:
        return file_meta, task_mediainfo

    # 远端识别均未命中时，仅单音轨子目录可安全按 Single 兜底；
    # 多音轨目录仍保持未识别，避免把缺失专辑误判为单曲。
    task_mediainfo.album_type = "Single"
    finalized = owner._finalize_recognition_result(task_mediainfo)
    if isinstance(finalized, MusicInfo):
        task_mediainfo = finalized
    if not task_mediainfo.library_category:
        # 隔离测试可能没有装配分类服务，保留旧分类路径作为测试兼容。
        task_mediainfo.set_library_category("Single")
    return file_meta, task_mediainfo
