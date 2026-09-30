"""音乐整理批次的本地标签准入、目录共识和曲目上下文编排。"""

import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional, Tuple, Union, cast

from app.application.audio import AudioMetadataHelper, capture_audio_metadata
from app.application.history import DownloadHistorySnapshot
from app.application.history.retry import max_failed_retries
from app.application.music.observation import BLOCKING_MUSIC_RECOGNITION_STATES, RETRYABLE_MUSIC_RECOGNITION_STATES
from app.application.transfer.workflow import TransferPlanningInput, TransferTask
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic, music_credit_values
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
    album_evidence_by_main_key: dict[Tuple[str, str], MetaMusic] = field(default_factory=dict)
    resolved_contexts: dict[Tuple[str, str], tuple[MetaMusic, MusicInfo]] = field(default_factory=dict)
    tags_by_file: dict[Tuple[str, str], Optional[MetaMusic]] = field(default_factory=dict)
    release_by_main_key: dict[Tuple[str, str], "MusicReleaseGroup"] = field(default_factory=dict)
    cue_by_main_key: dict[Tuple[str, str], MetaMusic] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MusicReleaseGroup:
    """同一物理发行单元的文件范围和仅供专辑级补缺的标签共识。"""

    directory: Path
    files: tuple[FileItem, ...]
    evidence: Optional[MetaMusic]

    @property
    def paths(self) -> list[Path]:
        """返回完整音轨路径列表，路径失效时拒绝悄悄缩小识别范围。"""
        return [_release_item_path(item) for item in self.files]


def music_planning_input(
        owner: _TransferOwnerBase, task: TransferTask, context: MusicBatchContext,
        cleanup: Optional[FileItem], regions: Optional[list[str]], scripts: Optional[list[str]],
) -> TransferPlanningInput:
    """冻结需要重新识别的原发行范围，重启恢复不能扩大到未选择的其他专辑。"""
    planning = cast(TransferPlanningInput, owner._TransferChain__build_planning_input(task, cleanup_dest_fileitem=cleanup))
    if not isinstance(task.meta, MetaMusic):
        return planning
    recognition = task.mediainfo.raw_data.get("recognition", {}) if isinstance(task.mediainfo, MusicInfo) else {}
    recognition = recognition if isinstance(recognition, dict) else {}
    if task.mediainfo and not task.meta.organization_error and recognition.get("status") not in BLOCKING_MUSIC_RECOGNITION_STATES:
        return planning
    if task.fileitem.storage != "local":
        return replace(planning, options={**planning.options, "music_recognition_scope": {
            "name_only": True, "storage": task.fileitem.storage,
        }})
    key = owner._get_file_key(task.fileitem)
    main_key = context.related_main_keys.get(key, key)
    group = context.release_by_main_key.get(main_key)
    if not group:
        return planning
    directory = group.directory.absolute()
    scope: dict[str, Any] = {
        "storage": "local", "directory": str(directory), "main": str(Path(main_key[1]).absolute().relative_to(directory)),
        "files": [str(path.absolute().relative_to(directory)) for path in group.paths],
        "regions": regions, "scripts": scripts,
    }
    return replace(planning, options={**planning.options, "music_recognition_scope": scope})


def _music_retry_files(task: TransferTask, scope: dict[str, Any]) -> tuple[list[FileItem], FileItem]:
    """恢复已选音频范围并拒绝越过当前发行目录的路径。"""
    current = Path(str(task.fileitem.path)).absolute()
    directory = Path(str(scope.get("directory", ""))).absolute()
    expected = current.parent.parent if MetaMusic.parse_disc_dir(current.parent.name) else current.parent
    if directory != expected or task.fileitem.storage != "local":
        raise ValueError("音乐重试目录与源文件不一致，请重新选择文件")
    names = scope.get("files")
    if not isinstance(names, list) or not names:
        raise ValueError("音乐重试缺少原文件范围，请重新选择文件")
    files = []
    for name in names:
        relative = Path(str(name))
        path = directory / relative
        if (relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(directory)
                or (path.parent != directory and not (
                    path.parent.parent == directory and MetaMusic.parse_disc_dir(path.parent.name)))):
            raise ValueError("音乐重试文件超出原发行范围，请重新选择文件")
        files.append(FileItem(storage="local", path=str(path), type="file", name=path.name,
                              basename=path.stem, extension=path.suffix.lstrip(".")))
    main = directory / str(scope.get("main", ""))
    item = next((item for item in files if item.path == str(main)), None)
    if item is None or (current != main and current.parent != main.parent):
        raise ValueError("音乐重试主音频与源文件不一致，请重新选择文件")
    return files, item


def _music_retry_preferences(scope: dict[str, Any], key: str) -> Optional[list[str]]:
    """校验冻结的发行偏好，损坏的恢复输入不能悄悄改变候选选择。"""
    values = scope.get(key)
    if values is None:
        return None
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("音乐发行偏好数据无效，请重新选择文件")
    return cast(list[str], list(values))


@capture_audio_metadata()
def refresh_music_retry_context(owner: _TransferOwnerBase, task: TransferTask) -> None:
    """仅在未规划的 accepted 重放中重新识别，已有检查点必须沿用冻结身份和路径。"""
    planning = task.planning_input
    scope = planning.options.get("music_recognition_scope") if planning else None
    if task.plan_checkpoint is not None or not task.planning_context_restored:
        return
    if planning and planning.options.get("music_replan_classification") and isinstance(task.mediainfo, MusicInfo):
        task.mediainfo = owner._finalize_recognition_result(task.mediainfo, refresh=True)
        return
    if not isinstance(scope, dict):
        return
    try:
        if scope.get("storage") != task.fileitem.storage:
            raise ValueError("音乐重试存储与原任务不一致，请重新选择文件")
        if scope.get("name_only"):
            path = Path(str(task.fileitem.path))
            history = owner._resolve_download_history(repository=owner.download_history_repository,
                                                      file_path=path, bluray_dir=False, download_hash=task.download_hash)
            task.meta = restore_music_resource_meta(history, path, AudioMetadataHelper.read_filename(path),
                                                   storage=task.fileitem.storage)
            task.mediainfo = None
            return
        files, main = _music_retry_files(task, scope)
        main_path = Path(str(main.path))
        context = prepare_music_batch_context(owner, [(item, False) for item in files], MediaType.MUSIC)
        meta = AudioMetadataHelper.read(main_path)
        if main.path != task.fileitem.path:
            related = owner._get_related_main_file_key(task.fileitem, files)
            cue_related = meta.music_layout == "image_cue" and meta.cue_filename == task.fileitem.name
            if not cue_related and related != owner._get_file_key(main):
                raise ValueError("关联文件与音频的对应关系已改变，请重新预览")
        # 下载时的原种子线索仍有效，但不能恢复上次失败或误选的媒体身份。
        history = owner._resolve_download_history(repository=owner.download_history_repository,
                                                  file_path=main_path, bluray_dir=False, download_hash=task.download_hash)
        meta = restore_music_resource_meta(history, main_path, meta) or meta
        task.meta, task.mediainfo = resolve_music_batch_file_context(
            owner, batch_context=context, file_item=main, file_path=main_path, file_meta=meta,
            selected_tracks={}, fallback=None, discard_shared_identity=False,
            multi_track_batch=len(files) > 1, release_regions=_music_retry_preferences(scope, "regions"),
            release_scripts=_music_retry_preferences(scope, "scripts"),
        )
    except (TypeError, ValueError) as error:
        info = MusicInfo.from_meta(task.meta) if isinstance(task.meta, MetaMusic) else MusicInfo()
        info.raw_data["recognition"] = {"status": "conflict", "message": str(error)}
        task.mediainfo = info


def defer_music_recognition(owner: _TransferOwnerBase, task: TransferTask) -> Optional[tuple[bool, str]]:
    """临时来源故障保留未规划任务并有界退避，预算耗尽后交给普通失败结算。"""
    if task.plan_checkpoint is not None or not isinstance(task.mediainfo, MusicInfo):
        return None
    if not task.planning_input or not task.planning_input.options.get("music_recognition_scope"):
        return None
    recognition = task.mediainfo.raw_data.get("recognition")
    if not isinstance(recognition, dict) or recognition.get("status") not in RETRYABLE_MUSIC_RECOGNITION_STATES:
        return None
    message = str(recognition.get("message") or "音乐识别暂时不可用")
    if task.preview:
        return False, message
    owner._TransferChain__claim_task_for_execution(task)
    owner._TransferChain__assert_owned_lease(task)
    if owner._transfer_admissions.defer_planning(
        task_id=task.admission_task_id, lease_token=task.lease_token, error=message,
        retry_after=30, max_retries=max_failed_retries(),
    ):
        owner._TransferChain__forget_owned_lease(task.admission_task_id, task.lease_token)
        owner._TransferChain__ensure_recovery_scheduler(immediate=False)
        return False, f"{message}；已安排自动重新识别"
    recognition["message"] = f"{message}；自动重试已达上限，请重新识别或手动选择专辑"
    return None


def _music_file_key(item: FileItem) -> Tuple[str, str]:
    """按存储与规范路径隔离标签快照，远端同名路径不能借用本机文件标签。"""
    return item.storage or "local", str(Path(item.path)) if item.path else ""


def _release_item_path(item: FileItem) -> Path:
    """发行分组只接受已经解析的实际文件路径，不能把空路径解释成当前目录。"""
    if not item.path:
        raise ValueError("发行分组中的音轨缺少路径")
    return Path(item.path)


def append_cue_companions(
        owner: _TransferOwnerBase,
        items: list[tuple[FileItem, bool]],
        inherited: dict[Tuple[str, str], Any],
        video_source: bool,
        exclude_words: Any,
) -> tuple[list[tuple[FileItem, bool]], dict[Tuple[str, str], Any]]:
    """把有效整轨 CUE 排在其音频后归档；分轨 CUE 仅供识别，避免复制失效引用。"""
    if video_source:
        return items, inherited
    result = list(items)
    known = {owner._get_file_key(item) for item, _ in items}
    for item, bluray in items:
        if bluray or item.storage != "local" or not item.path or not owner._is_audio_file(item):
            continue
        path = Path(item.path)
        meta = AudioMetadataHelper.read(path)
        if meta.music_layout != "image_cue" or not meta.cue_filename or meta.organization_error:
            continue
        cue_path = path.parent / meta.cue_filename
        if owner._is_blocked_by_exclude_words(str(cue_path), exclude_words):
            logger.info(f"{cue_path.name} 被用户的整理过滤规则排除，保留源索引")
            continue
        cue_item = owner._transfer_storage_chain().get_item(FileItem(
            storage="local", path=str(cue_path), type="file", name=cue_path.name,
            basename=cue_path.stem, extension=cue_path.suffix.lstrip("."),
        ))
        if not cue_item:
            meta.organization_error = "关联 CUE 已不可读取，请重新预览"
            inherited[owner._get_file_key(item)] = meta
            continue
        key = owner._get_file_key(cue_item)
        if key not in known:
            result.append((cue_item, False))
            known.add(key)
        inherited[key] = deepcopy(meta)
    return result, inherited


def restore_music_resource_meta(
        history: Optional[DownloadHistorySnapshot],
        path: Path,
        file_meta: Optional[MetaMusic] = None,
        *,
        storage: Optional[str] = "local",
) -> Optional[MetaMusic]:
    """从原始种子主副标题补足文件的发行线索，不复用已丢弃的远端身份。

    专辑字段只传到下载根及其碟片目录；全集或更深的独立专辑目录只继承
    可信艺人。文件曲名、曲序和已有标签不被整包标题覆盖。
    """
    title = str(getattr(history, "torrent_name", None) or "")
    description = str(getattr(history, "torrent_description", None) or "")
    history_type = getattr(history, "type", None)
    if history_type in (MediaType.MOVIE, MediaType.TV, MediaType.MOVIE.value, MediaType.TV.value):
        return file_meta
    if not title and not description:
        return file_meta
    root_text = getattr(history, "path", None)
    root = Path(root_text) if root_text else None
    if root and root != path and not path.is_relative_to(root):
        return file_meta
    resource = MetaMusic.parse_resource(title, description)
    meta = deepcopy(file_meta) if file_meta else (
        AudioMetadataHelper.read(path) if storage == "local" and path.is_file() else AudioMetadataHelper.read_filename(path)
    )
    parent = path.parent.parent if MetaMusic.parse_disc_dir(path.parent.name) else path.parent
    direct_release = bool(root and (root == path or root == parent))
    collection = bool(re.search(
        r"全集|专辑合集|專輯合集|\bdiscography\b|\bcomplete\s+(?:albums?|collection)\b|\d+\s*张专辑",
        f"{title} {description}", re.IGNORECASE,
    ))
    collective = {music_text_key(value) for value in ("Various Artists", "Various", "VA", "群星", "众艺人", "眾藝人")}
    artists = [artist for artist in resource.artists if music_text_key(artist) not in collective]
    if not meta.artists and artists:
        meta.artists = list(artists)
        meta.field_sources["artists"] = "torrent"
    if not meta.album_artist and len(resource.artists) == 1:
        meta.album_artist = resource.artists[0]
        meta.field_sources["album_artist"] = "torrent"
    if direct_release and not collection:
        for key, value in music_credit_values(resource).items():
            if value and not getattr(meta, key):
                setattr(meta, key, value)
                meta.field_sources[key] = "torrent"
        album = resource.album
        if root != path and not resource.track_number:
            album = album or resource.title
        if not meta.album and album:
            meta.album = album
            meta.field_sources["album"] = "torrent"
        if not meta.year and resource.year:
            meta.year = resource.year
            meta.field_sources["year"] = "torrent"
        if not meta.version and resource.version:
            meta.version = resource.version
            meta.field_sources["version"] = "torrent"
    return meta


def _music_directory_consensus(values: list[str], *, artist: bool = False) -> Optional[str]:
    """只在同目录至少两个标签高度一致时返回共识值。"""
    normalized = [
        ("variousartists" if artist and _collective_artist(value) else music_text_key(value), value.strip())
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


def _music_directory_evidence(
        items: list[FileItem], tags_by_file: dict[Tuple[str, str], Optional[MetaMusic]],
) -> Optional[MetaMusic]:
    """从同一发行目录的原始音频标签提取可信艺人与专辑共识。"""
    artist_values: list[str] = []
    track_artists: list[str] = []
    album_values: list[str] = []
    metas: list[MetaMusic] = []
    for item in items:
        if getattr(item, "storage", "local") != "local" or not item.path:
            continue
        tag_meta = tags_by_file.get(_music_file_key(item))
        if not tag_meta:
            continue
        artist = _album_artist(tag_meta, _release_directory(item))
        if artist:
            artist_values.append(artist)
        if tag_meta.album:
            album_values.append(tag_meta.album)
        if len(tag_meta.artists) == 1:
            track_artists.append(tag_meta.artists[0])
        metas.append(tag_meta)
    artist = _music_directory_consensus(artist_values, artist=True)
    track_artist = _music_directory_consensus(track_artists, artist=True)
    album = _music_directory_consensus(album_values)
    if not artist and not album:
        return None
    evidence = MetaMusic(
        artists=[track_artist] if track_artist and not _collective_artist(track_artist) else None,
        album_artist=artist,
        album=album,
    )
    for key in ("musicbrainz_release_id", "musicbrainz_release_group_id", "album_type", "original_year", "release_year"):
        values = {getattr(meta, key) for meta in metas if getattr(meta, key)}
        if len(values) == 1:
            setattr(evidence, key, next(iter(values)))
    secondary = {tuple(meta.secondary_types) for meta in metas if meta.secondary_types}
    if len(secondary) == 1:
        evidence.secondary_types = list(next(iter(secondary)))
    discs = [meta.total_discs or meta.disc_number or 1 for meta in metas]
    discs.extend(MetaMusic.parse_disc_dir(_release_item_path(item).parent.name) or 1 for item in items)
    evidence.total_discs = max(discs, default=1) if max(discs, default=1) > 1 else None
    if evidence.total_discs:
        evidence.field_sources["total_discs"] = (
            "album_tags" if any(meta.total_discs == evidence.total_discs for meta in metas) else "directory"
        )
    return evidence


def _collective_artist(value: Optional[str]) -> bool:
    """识别专辑层的群星署名，不把它复制成每首歌曲的演唱者。"""
    return music_text_key(value) in {
        music_text_key(name) for name in ("Various Artists", "Various", "VA", "群星", "众艺人", "眾藝人")
    }


def _album_artist(meta: MetaMusic, directory: Path) -> Optional[str]:
    """专辑署名优先读 Album Artist，明确的合辑标志可补群星，但不改逐曲艺人。"""
    if meta.album_artist:
        return meta.album_artist
    if "Compilation" in meta.secondary_types:
        return "Various Artists"
    parsed = MetaMusic.parse_album_dir(directory.name)
    if _collective_artist(parsed.get("artist")) and music_text_key(parsed.get("album")) == music_text_key(meta.album):
        return cast(str, parsed["artist"])
    return meta.artists[0] if len(meta.artists) == 1 else None


def _release_signature(meta: MetaMusic, directory: Path) -> tuple[str, str, Any]:
    """缺少发行 ID 时，以专辑署名、标题和当前年份限定可关联的标签。"""
    artist = _album_artist(meta, directory)
    artist_key = "variousartists" if _collective_artist(artist) else music_text_key(artist)
    return artist_key, music_text_key(meta.album), meta.release_year or meta.year


def _rendition_groups(group: list[FileItem], tags: dict[Tuple[str, str], Optional[MetaMusic]]) -> list[list[FileItem]]:
    """同一曲序存在多份时按实际编码区分版本，正常混合介质的多碟仍保持一组。"""
    positions = Counter(
        (meta.disc_number or MetaMusic.parse_disc_dir(_release_item_path(item).parent.name) or 1, meta.track_number)
        for item in group if (meta := tags.get(_music_file_key(item))) and meta.track_number
    )
    if not any(count > 1 for count in positions.values()):
        return [group]
    renditions: dict[tuple[Any, ...], list[FileItem]] = {}
    for item in group:
        meta = tags.get(_music_file_key(item))
        key = (meta.audio_format, meta.bit_depth, meta.sample_rate) if meta else (None,)
        renditions.setdefault(key, []).append(item)
    return list(renditions.values())


def _release_groups(items: list[FileItem], tags_by_file: dict[Tuple[str, str], Optional[MetaMusic]]) -> list[MusicReleaseGroup]:
    """优先按具体发行 ID 分组，缺 ID 时按专辑署名、标题及发行年隔离同目录多专辑。"""
    grouped: dict[tuple[Any, ...], list[FileItem]] = {}
    unassigned: list[FileItem] = []
    known: dict[tuple[str, str, Any], set[str]] = {}
    for item in items:
        meta = tags_by_file.get(_music_file_key(item))
        if meta and meta.musicbrainz_release_id:
            known.setdefault(_release_signature(meta, _release_directory(item)), set()).add(meta.musicbrainz_release_id)
    for item in items:
        meta = tags_by_file.get(_music_file_key(item))
        if not meta:
            unassigned.append(item)
            continue
        signature = _release_signature(meta, _release_directory(item))
        matching_ids = known.get(signature, set())
        key: tuple[Any, ...]
        if meta.musicbrainz_release_id:
            key = ("release", meta.musicbrainz_release_id)
        elif len(matching_ids) == 1:
            key = ("release", next(iter(matching_ids)))
        elif signature[0] and signature[1]:
            key = ("tags", *signature)
        else:
            unassigned.append(item)
            continue
        grouped.setdefault(key, []).append(item)
    if len(grouped) == 1:
        existing = next(iter(grouped.values()))
        album_names = {music_text_key(meta.album) for item in existing if (meta := tags_by_file.get(_music_file_key(item))) and meta.album}
        album_artists = {_album_artist(meta, _release_directory(item)) for item in existing
                         if (meta := tags_by_file.get(_music_file_key(item)))}
        artist_keys = {music_text_key(artist) for artist in album_artists if artist}
        collective = any(_collective_artist(artist) for artist in album_artists)
        directory = MetaMusic.parse_album_dir(_release_directory(existing[0]).name)
        directory_matches = bool(
            (directory.get("artist") or directory.get("year"))
            and music_text_key(directory.get("album")) in album_names
        )
        enough_tags = len(existing) * 2 >= len(items) or directory_matches
        remaining = []
        for item in unassigned:
            meta = tags_by_file.get(_music_file_key(item))
            if not enough_tags:
                remaining.append(item)
                continue
            if meta and not meta.album and meta.artists and not collective and not (
                artist_keys & {music_text_key(artist) for artist in meta.artists}
            ):
                remaining.append(item)
                continue
            if not meta or not meta.album or music_text_key(meta.album) in album_names:
                existing.append(item)
            else:
                remaining.append(item)
        unassigned = remaining
    if unassigned:
        grouped[("unassigned",)] = unassigned
    output = []
    for group in grouped.values():
        for rendition in _rendition_groups(group, tags_by_file):
            root = _release_directory(rendition[0])
            evidence = _music_directory_evidence(rendition, tags_by_file)
            output.append(MusicReleaseGroup(root, tuple(rendition), evidence))
    return output


def _release_directory(item: FileItem) -> Path:
    """把 CD/Disc 子目录定位到所属专辑根，不跨越其它子专辑目录。"""
    parent = _release_item_path(item).parent
    return parent.parent if MetaMusic.parse_disc_dir(parent.name) is not None else parent


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
        merged.field_sources["artists"] = "album_tags"
    if not merged.album_artist and evidence.album_artist:
        merged.album_artist = evidence.album_artist
        merged.field_sources["album_artist"] = "album_tags"
    if not merged.album and evidence.album:
        merged.album = evidence.album
        merged.field_sources["album"] = "album_tags"
    for key in ("musicbrainz_release_id", "musicbrainz_release_group_id", "total_discs", "album_type",
                "original_year", "release_year"):
        if not getattr(merged, key) and getattr(evidence, key):
            setattr(merged, key, getattr(evidence, key))
            merged.field_sources[key] = evidence.field_sources.get(key, "album_tags")
    if not merged.secondary_types and evidence.secondary_types:
        merged.secondary_types = list(evidence.secondary_types)
        merged.field_sources["secondary_types"] = "album_tags"
    if not merged.year and (merged.release_year or merged.original_year):
        merged.year = cast(Any, merged.release_year or merged.original_year)
        merged.field_sources["year"] = merged.field_sources.get("release_year", merged.field_sources.get("original_year", "album_tags"))
    return merged


def _apply_music_directory_year(meta: MetaMusic, file_path: Path) -> MetaMusic:
    """保留明确的标签发行年，仅用目录年份补正旧版未区分语义的 year。"""
    if meta.release_year and meta.field_sources.get("release_year") in {"tag", "album_tags"}:
        merged = deepcopy(meta)
        merged.year = cast(Any, meta.release_year)
        merged.field_sources["year"] = meta.field_sources["release_year"]
        return merged
    for parent in list(file_path.parents)[:4]:
        match = re.match(r"^((?:19|20)\d{2})(?:\D|$)", parent.name)
        if not match:
            continue
        merged = deepcopy(meta)
        merged.year = cast(Any, int(match.group(1)))
        merged.field_sources["year"] = "directory"
        return merged
    return meta


def _local_music_context(
        owner: _TransferOwnerBase,
        file_item: FileItem,
        file_path: Path,
        batch_context: MusicBatchContext,
        file_meta: MetaMusic,
) -> Optional[tuple[MetaMusic, MusicInfo]]:
    """仅以本地真实标签建立快速整理上下文，不向在线来源确认身份。"""
    if file_item.storage != "local" or not owner._is_audio_file(file_item):
        return None
    if file_meta.organization_error:
        return file_meta, MusicInfo.from_meta(file_meta)
    if file_meta.music_layout in {"image_cue", "tracks_cue"} and music_tags_are_usable(file_meta):
        return file_meta, MusicInfo.from_meta(file_meta)
    tags = batch_context.tags_by_file.get(_music_file_key(file_item)) if _music_file_key(file_item) in batch_context.tags_by_file else AudioMetadataHelper.read_tags(file_path)
    if tags:
        tags = deepcopy(tags)
        if owner._get_file_key(file_item) in batch_context.album_main_keys:
            tags = _apply_music_directory_evidence(tags, batch_context.album_evidence_by_main_key.get(owner._get_file_key(file_item)))
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
    if batch_mtype not in (None, MediaType.MUSIC):
        return context
    main_items_by_dir: dict[Tuple[str, str], list[FileItem]] = {}
    release_items: dict[Tuple[str, str], list[FileItem]] = {}
    for current_item, current_bluray_dir in file_items:
        if not current_bluray_dir and owner._is_media_file(current_item, MediaType.MUSIC):
            main_items_by_dir.setdefault(
                owner._get_file_parent_key(current_item), []
            ).append(current_item)
            if current_item.path:
                release_items.setdefault((current_item.storage or "local", str(_release_directory(current_item))), []).append(current_item)
                if current_item.storage == "local":
                    context.tags_by_file[_music_file_key(current_item)] = AudioMetadataHelper.read_tags(Path(current_item.path))
                    tags = context.tags_by_file[_music_file_key(current_item)] or AudioMetadataHelper.read_filename(Path(current_item.path))
                    cue_meta = AudioMetadataHelper.with_cue_context(Path(current_item.path), tags)
                    if cue_meta.music_layout:
                        context.cue_by_main_key[owner._get_file_key(current_item)] = cue_meta
    context.single_main_keys = {
        owner._get_file_key(items[0])
        for items in release_items.values()
        if len(items) == 1
    }
    for items in release_items.values():
        for group in _release_groups(items, context.tags_by_file):
            evidence = group.evidence
            for item in group.files:
                item_key = owner._get_file_key(item)
                context.release_by_main_key[item_key] = group
                if evidence:
                    context.album_evidence_by_main_key[item_key] = evidence
                if evidence and len(group.files) > 1 and evidence.album and (evidence.album_artist or evidence.artists):
                    context.album_main_keys.add(item_key)
    for current_item, _current_bluray_dir in file_items:
        if str(current_item.extension or "").casefold() == "cue":
            for main_key, cue_meta in context.cue_by_main_key.items():
                if (cue_meta.music_layout == "image_cue" and current_item.storage == main_key[0]
                        and current_item.path == str(Path(main_key[1]).parent / str(cue_meta.cue_filename))):
                    context.related_main_keys[owner._get_file_key(current_item)] = main_key
        if not owner._is_music_lyrics_file(current_item):
            continue
        related_key = owner._get_related_main_file_key(
            current_item,
            main_items_by_dir.get(owner._get_file_parent_key(current_item), []),
        )
        if related_key:
            context.related_main_keys[owner._get_file_key(current_item)] = related_key
    return context


def _finalize_music_group_context(
        owner: _TransferOwnerBase,
        context: MusicBatchContext,
        item: FileItem,
        meta: Any,
        info: Optional[Union[MediaInfo, MusicInfo]],
        *,
        local: bool,
        preserve_selection: bool,
) -> tuple[Any, Optional[Union[MediaInfo, MusicInfo]]]:
    """统一同一发行的专辑名称与分类，保留各曲艺人及用户明确选择。"""
    if not isinstance(meta, MetaMusic) or not isinstance(info, MusicInfo):
        return meta, info
    recognition = info.raw_data.get("recognition")
    if isinstance(recognition, dict) and recognition.get("status") in BLOCKING_MUSIC_RECOGNITION_STATES:
        return meta, info
    key = owner._get_file_key(item)
    grouped = key in context.album_main_keys and not preserve_selection
    if grouped:
        meta, info = deepcopy(meta), deepcopy(info)
        evidence = context.album_evidence_by_main_key.get(key)
        for name in ("album", "album_artist", "total_discs"):
            value = getattr(evidence, name, None)
            if value:
                setattr(meta, name, value)
                setattr(info, name, value)
                source = evidence.field_sources.get(name, "album_tags") if evidence else "album_tags"
                meta.field_sources[name] = info.field_sources[name] = source
        if meta.year:
            info.year = meta.year
        if meta.album_type:
            info.album_type = meta.album_type
            info.field_sources["album_type"] = meta.field_sources.get("album_type", "album_tags")
        elif str(info.album_type or "").casefold() not in {"ep", "broadcast", "other"}:
            # 未声明具体类型的多音轨标签共识沿用 Album 兜底，不能覆盖已声明的 EP/Single。
            info.album_type = "Album"
            info.field_sources["album_type"] = "album_tags"
        if meta.secondary_types:
            info.secondary_types = list(meta.secondary_types)
            info.field_sources["secondary_types"] = meta.field_sources.get("secondary_types", "album_tags")
    if local or grouped:
        finalized = owner._finalize_recognition_result(
            info, **({"allow_enrichment": False} if local else {"refresh": True}),
        )
        if isinstance(finalized, MusicInfo):
            info = finalized
        if not local and grouped and not info.classification and info.album_type == "Album":
            # 没有装配分类服务的旧调用方保留兼容目录；真实策略结果不能被硬编码覆盖。
            info.set_library_category("Album")
    return meta, info


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
    selected_context = task_mediainfo is not None
    cue_meta = batch_context.cue_by_main_key.get(file_key)
    if cue_meta:
        incoming_error = file_meta.organization_error if isinstance(file_meta, MetaMusic) else None
        if selected_context and isinstance(file_meta, MetaMusic):
            file_meta = deepcopy(file_meta)
            for name in ("music_layout", "cue_filename", "cue_tracks", "organization_error"):
                setattr(file_meta, name, deepcopy(getattr(cue_meta, name)))
            if cue_meta.music_layout == "image_cue":
                file_meta.track_number = None
                if isinstance(task_mediainfo, MusicInfo) and task_mediainfo.music_type != "album":
                    file_meta.organization_error = "整轨 CUE 包含多首歌曲，请选择专辑身份整理"
        else:
            file_meta = deepcopy(cue_meta)
        if incoming_error:
            file_meta.organization_error = incoming_error
    local_context = (
        _local_music_context(owner, file_item, file_path, batch_context, file_meta)
        if not task_mediainfo and isinstance(file_meta, MetaMusic)
        else None
    )
    if local_context:
        file_meta, task_mediainfo = local_context
    if isinstance(file_meta, MetaMusic):
        file_key = owner._get_file_key(file_item)
        file_meta = _apply_music_directory_evidence(
            file_meta,
            batch_context.album_evidence_by_main_key.get(file_key),
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
    file_meta, task_mediainfo = _finalize_music_group_context(
        owner, batch_context, file_item, file_meta, task_mediainfo,
        local=bool(local_context), preserve_selection=selected_context,
    )
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
        release_group=batch_context.release_by_main_key.get(owner._get_file_key(file_item)),
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
        task_mediainfo.album_type = file_meta.album_type or "Album"
        finalized = owner._finalize_recognition_result(task_mediainfo, allow_enrichment=False)
        if isinstance(finalized, MusicInfo):
            task_mediainfo = finalized
        if not task_mediainfo.library_category and not task_mediainfo.classification:
            task_mediainfo.set_library_category(task_mediainfo.album_type)
        return file_meta, task_mediainfo
    if owner._get_file_key(file_item) not in batch_context.single_main_keys:
        return file_meta, task_mediainfo

    # 远端识别均未命中时，仅单音轨子目录可安全按 Single 兜底；
    # 多音轨目录仍保持未识别，避免把缺失专辑误判为单曲。
    task_mediainfo.album_type = file_meta.album_type or "Single"
    finalized = owner._finalize_recognition_result(task_mediainfo, allow_enrichment=False)
    if isinstance(finalized, MusicInfo):
        task_mediainfo = finalized
    if not task_mediainfo.library_category and not task_mediainfo.classification:
        # 隔离测试可能没有装配分类服务，保留旧分类路径作为测试兼容。
        task_mediainfo.set_library_category(task_mediainfo.album_type)
    return file_meta, task_mediainfo
