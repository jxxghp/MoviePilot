"""下载器已有任务的媒体识别、资源归类与根目录重命名。"""

import re
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from app.application.configuration import get_configured_system_config
from app.application.directory import DirectoryHelper, validate_download_save_path
from app.domain.classification.validation import validate_classification_category_path
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.schemas.types import (
    MUSIC_ARTIST_COLLECTION_CATEGORY,
    MediaSource,
    MediaType,
    SystemConfigKey,
)

_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_MUSIC_PRIMARY_TYPES = {
    "album": "Album",
    "ep": "EP",
    "single": "Single",
    "broadcast": "Broadcast",
    "other": "Other",
}


def _safe_relative_name(value: Any, *, label: str) -> str:
    """把识别结果规范为单层路径名，避免展示分隔符变成真实目录。"""
    text = _INVALID_NAME.sub(" - ", str(value or "").strip())
    text = " ".join(text.split()).strip(" .")
    if not text or text in (".", ".."):
        raise ValueError(f"{label}为空或无法生成安全名称")
    return text


def _source_value(value: Any) -> str | None:
    """把媒体来源枚举或字符串归一化，供来源与 ID 成对比较。"""
    normalized = str(getattr(value, "value", value) or "").strip()
    return normalized.casefold() or None


def _local_path(value: Any, *, label: str, validate: bool = False) -> PurePath:
    """按 POSIX/Windows 风格解析本地路径，并统一输出正斜杠形式。"""
    text = str(value or "").strip()
    if _WINDOWS_DRIVE_PATH.match(text):
        text = text.replace("\\", "/")
    if validate:
        text = validate_download_save_path(text)
    path: PurePath
    if _WINDOWS_DRIVE_PATH.match(text):
        path = PureWindowsPath(text)
    else:
        path = PurePosixPath(text)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label}无效")
    return path


def _normalize_music_category(media: Any) -> tuple[str, list[str]]:
    """优先使用已生效分类路径，缺失时兼容退回音乐主类型。"""
    if str(getattr(media, "music_type", None) or "").casefold() == "artist":
        return MUSIC_ARTIST_COLLECTION_CATEGORY, []
    classified_path = DirectoryHelper().resolve_media_category(media).path
    if classified_path:
        path = validate_classification_category_path(classified_path)
        category = "/".join(path)
    else:
        category = ""
    primary = str(getattr(media, "album_type", None) or "").strip()
    if not primary:
        primary = str(getattr(media, "category", None) or "").split("/")[0].strip()
    if not primary and str(getattr(media, "music_type", None) or "").casefold() == "recording":
        primary = "Single"
    primary = _MUSIC_PRIMARY_TYPES.get(primary.casefold(), primary)
    secondary = [
        str(item).strip()
        for item in (getattr(media, "secondary_types", None) or [])
        if str(item).strip() and str(item).strip() != primary
    ]
    return category or _safe_relative_name(primary, label="音乐主类型"), secondary


def _resolve_media(request: Any, history: Any, torrent: Any, media_chain: Any) -> tuple[MetaBase, Any]:
    """复用 MoviePilot 的媒体识别链，支持自动识别和显式原生媒体 ID。"""
    raw_type = request.type_name or history.type
    try:
        media_type = MediaType(raw_type) if raw_type else None
    except ValueError as error:
        raise ValueError("下载历史缺少有效媒体类型，请手动选择") from error
    title = str(torrent.title or history.torrent_name or history.title or "").strip()
    if not title:
        raise ValueError("任务缺少可用于识别的名称")
    is_music = media_type == MediaType.MUSIC
    metainfo = (
        MetaMusic.parse_query(title)
        if is_music
        else MetaInfo(title=title, subtitle=history.torrent_description)
    )
    history_source = getattr(history, "media_source", None)
    source = request.media_source or history_source
    media_id = request.media_id
    if (
        not media_id
        and source
        and _source_value(source) == _source_value(history_source)
    ):
        media_id = getattr(history, "media_id", None)
    music_type = (
        request.music_type.value
        if getattr(request.music_type, "value", None)
        else request.music_type or getattr(history, "music_type", None) or ("album" if is_music else None)
    )
    # 媒体链在未显式给出音乐源时会按单曲路由；资源根目录默认按专辑识别。
    if is_music and not source:
        source = MediaSource.MusicBrainz
    if source and media_id:
        media = media_chain.recognize_media(
            meta=metainfo,
            mtype=media_type,
            media_source=source,
            media_id=media_id,
            episode_group=request.episode_group or getattr(history, "episode_group", None),
            music_type=music_type,
        )
    else:
        media = media_chain.recognize_by_meta(
            metainfo,
            mtype=media_type,
            media_source=source,
            episode_group=request.episode_group or getattr(history, "episode_group", None),
            obtain_images=False,
            music_type=music_type,
        )
    if media is None:
        raise ValueError("无法识别媒体信息，可搜索并指定媒体 ID，或改用手动指定目录")
    return metainfo, media


def _download_root(current: PurePath, media_type: str, category: str) -> tuple[Any, PurePath]:
    """按媒体类型与主类别选择资源目录，优先保持在当前配置根内。"""
    candidates = []
    for directory in DirectoryHelper().get_download_dirs():
        if directory.storage != "local" or not directory.download_path:
            continue
        try:
            root = _local_path(directory.download_path, label="资源目录")
        except ValueError:
            continue
        if type(root) is not type(current):
            continue
        if directory.media_type and directory.media_type != media_type:
            continue
        if directory.media_category and directory.media_category != category:
            continue
        candidates.append((
            int(current.is_relative_to(root)),
            -int(directory.priority or 0),
            len(root.parts),
            directory,
            root,
        ))
    if not candidates:
        raise ValueError("没有找到匹配识别结果的本地资源目录")
    _, _, _, directory, root = max(candidates, key=lambda item: item[:3])
    target = root
    if not directory.media_type and directory.download_type_folder:
        target /= media_type
    if not directory.media_category and directory.download_category_folder:
        target /= category
    return directory, target


def _manual_target(value: str) -> PurePath:
    """校验手动目录是已配置资源目录本身或其子目录。"""
    return _local_path(value, label="手动目标路径", validate=True)


def _requested_category(value: Any, media_type: Any) -> str:
    """校验手动分类属于当前媒体类型的启用分类策略。"""
    path = validate_classification_category_path(
        tuple(segment.strip() for segment in str(value or "").split("/") if segment.strip())
    )
    if path not in DirectoryHelper().classification_category_paths(media_type):
        raise ValueError("手动指定的媒体分类不存在、已停用或与媒体类型不匹配")
    return "/".join(path)


def _root_name(media: Any) -> str:
    """生成跨电影、电视剧和音乐通用的规范任务根目录名。"""
    is_artist_collection = False
    if getattr(media, "type", None) == MediaType.MUSIC:
        if str(getattr(media, "music_type", None) or "").casefold() == "artist":
            is_artist_collection = True
            artist = getattr(media, "name", None) or getattr(media, "title", None)
            base = f"{str(artist or '').strip()} - 艺术家合集"
        else:
            title = getattr(media, "album", None) or getattr(media, "title", None)
            artist = getattr(media, "album_artist", None) or getattr(media, "artist", None)
            base = " - ".join(str(part).strip() for part in (artist, title) if str(part or "").strip())
    else:
        base = str(getattr(media, "title", None) or "").strip()
    year = str(getattr(media, "year", None) or "").strip()
    if year and not is_artist_collection and f"({year})" not in base:
        base = f"{base} ({year})"
    return _safe_relative_name(base, label="规范目录名")


def _downloader_kind(name: str) -> str | None:
    """返回已配置下载器类型，用于在预览阶段显式限定重命名能力。"""
    downloaders = get_configured_system_config().get(SystemConfigKey.Downloaders) or []
    match = next((item for item in downloaders if item.get("name") == name), None)
    return str(match.get("type") or "").casefold() if match else None


def _qb_module(chain: Any) -> Any:
    """返回当前运行的 qBittorrent 模块。"""
    return chain.modulemanager.get_running_module("QbittorrentModule")


def _qb_root_folder(
    chain: Any,
    downloader: str,
    hash_value: str,
    current: PurePath,
    content: PurePath | None,
) -> str | None:
    """确认 qB 任务是否拥有一个独立顶层目录。

    文件夹名称允许包含点号；不能使用 ``Path.suffix`` 猜测它是不是文件。
    优先检查已挂载文件系统，路径尚不存在时再用 qB 文件清单确认。
    """
    if _downloader_kind(downloader) != "qbittorrent" or not content:
        return None
    if not content.is_absolute() or not content.is_relative_to(current):
        return None
    relative_content = content.relative_to(current)
    if len(relative_content.parts) != 1:
        return None

    local_content = Path(content.as_posix())
    if local_content.is_dir():
        return relative_content.name
    if local_content.is_file():
        return None

    module = _qb_module(chain)
    try:
        torrent_files = module.torrent_files(tid=hash_value, downloader=downloader) if module else None
    except Exception:
        torrent_files = None
    file_paths = [
        PurePosixPath(str(getattr(item, "name", "")).replace("\\", "/"))
        for item in (torrent_files or [])
        if str(getattr(item, "name", "")).strip()
    ]
    if not file_paths or not all(len(path.parts) >= 2 for path in file_paths):
        return None
    top_levels = {path.parts[0] for path in file_paths}
    return relative_content.name if top_levels == {relative_content.name} else None


def _rename_qb_root(chain: Any, downloader: str, hash_value: str, old_name: str, new_name: str) -> bool:
    """通过已运行的 qBittorrent 模块调用官方 renameFolder API。

    此变更由下载器维护任务与文件的对应关系，不直接操作文件系统。
    """
    module = _qb_module(chain)
    server = module.get_instance(downloader) if module else None
    client = getattr(server, "qbc", None)
    if client is None:
        return False
    try:
        client.torrents_rename_folder(
            torrent_hash=hash_value,
            old_path=old_name,
            new_path=new_name,
        )
        return True
    except Exception:
        return False


def organize_existing_source(hash_value: str, request: Any, chain: Any, media_chain: Any) -> dict[str, Any]:
    """生成可重放的预览计划，确认后仅通过下载器修改任务路径。"""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", hash_value):
        raise ValueError("hash 格式无效")
    history = chain.download_history_repository.get_by_hash(hash_value)
    if history is None:
        raise ValueError("未找到该任务的下载历史")
    downloader = request.downloader or history.downloader
    matches = [
        item for item in (chain.list_torrents(
            hashs=[hash_value], downloader=downloader, include_all_tags=True
        ) or [])
        if str(item.hash or "").casefold() == hash_value.casefold()
    ]
    if len(matches) != 1:
        raise ValueError("无法唯一定位下载任务，请检查任务是否仍在下载器中")
    torrent = matches[0]
    downloader = downloader or torrent.downloader
    if not downloader:
        raise ValueError("无法确定下载器实例")
    current = _local_path(torrent.save_path, label="下载器返回的保存路径", validate=True)

    media = None
    category = None
    secondary_categories: list[str] = []
    if request.mode == "recognize" or request.smart_rename:
        _, media = _resolve_media(request, history, torrent, media_chain)
        media_type = media.type.value
        if request.media_category:
            category = _requested_category(request.media_category, media.type)
            if media.type == MediaType.MUSIC:
                _, secondary_categories = _normalize_music_category(media)
        elif media.type == MediaType.MUSIC:
            category, secondary_categories = _normalize_music_category(media)
        else:
            category = _safe_relative_name(
                getattr(media, "category", None) or history.media_category,
                label="媒体类别",
            )
    else:
        media_type = request.type_name or history.type

    if request.mode == "manual":
        target = _manual_target(str(request.target_path))
    else:
        if category is None:
            raise ValueError("识别结果缺少可用的媒体类别")
        _, target = _download_root(current, media_type, category)
        target = _local_path(target.as_posix(), label="目标保存路径", validate=True)

    content_text = str(torrent.content_path or torrent.path or "").strip()
    content = _local_path(content_text, label="下载器返回的内容路径") if content_text else None
    current_root_name = _qb_root_folder(chain, downloader, hash_value, current, content)
    rename_supported = current_root_name is not None
    proposed_root_name = _root_name(media) if request.smart_rename and media else current_root_name
    rename_required = bool(
        request.smart_rename
        and rename_supported
        and proposed_root_name
        and proposed_root_name != current_root_name
    )
    if request.smart_rename and not rename_supported:
        if _downloader_kind(downloader) != "qbittorrent":
            raise ValueError("智能根目录重命名目前仅支持 qBittorrent")
        raise ValueError("该 qBittorrent 任务没有可重命名的独立顶层目录；单文件或散列文件任务请关闭智能重命名")

    target_text = target.as_posix()
    changed = current.as_posix() != target_text or rename_required
    relocated = False
    renamed = False
    if request.execute and changed:
        expected = (
            request.expected_current_path == current.as_posix()
            and request.expected_target_path == target_text
            and request.expected_content_path == (content.as_posix() if content else None)
            and request.expected_root_name == proposed_root_name
        )
        if not expected:
            raise ValueError("任务路径或识别计划已变化，请重新预览后确认")
        if rename_required:
            if current_root_name is None or proposed_root_name is None:
                raise ValueError("根目录重命名计划不完整，请重新预览")
            renamed = _rename_qb_root(
                chain,
                downloader,
                hash_value,
                current_root_name,
                proposed_root_name,
            )
            if not renamed:
                raise ValueError("下载器未接受根目录重命名")
        if current.as_posix() != target_text:
            result = chain.update_torrent(
                hash_string=hash_value,
                downloader=downloader,
                save_path=target_text,
            ) or {}
            relocated = bool(result.get("save_path"))
            if not relocated:
                if renamed:
                    if current_root_name is None or proposed_root_name is None:
                        raise ValueError("根目录重命名计划不完整，无法自动回滚")
                    rolled_back = _rename_qb_root(
                        chain,
                        downloader,
                        hash_value,
                        proposed_root_name,
                        current_root_name,
                    )
                    if not rolled_back:
                        raise ValueError("保存位置修改失败，且根目录名无法自动回滚，请检查下载器")
                    renamed = False
                raise ValueError("下载器未接受保存位置修改，根目录重命名已回滚")

    media_source = getattr(media, "media_source", None)
    return {
        "hash": hash_value,
        "downloader": downloader,
        "mode": request.mode,
        "recognized": media is not None,
        "media_type": getattr(getattr(media, "type", None), "value", media_type),
        "media_source": getattr(media_source, "value", media_source),
        "media_id": str(getattr(media, "media_id", None) or "") or None,
        "title": getattr(media, "album", None) or getattr(media, "title", None),
        "year": str(getattr(media, "year", None) or "") or None,
        "current_save_path": current.as_posix(),
        "target_save_path": target_text,
        "current_content_path": content.as_posix() if content else None,
        "category": category,
        "secondary_categories": secondary_categories,
        "current_root_name": current_root_name,
        "proposed_root_name": proposed_root_name,
        "rename_supported": rename_supported,
        "rename_required": rename_required,
        "changed": changed,
        "executed": bool(request.execute and changed),
        "relocated": relocated,
        "renamed": renamed,
    }
