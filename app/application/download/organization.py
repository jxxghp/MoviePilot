"""下载器已有任务的媒体识别、资源归类与根目录重命名。"""

import json
import re
import time
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Protocol
from uuid import uuid4

from app.application.configuration import get_configured_system_config
from app.application.directory import (
    DirectoryHelper,
    build_media_download_path,
    source_directory_media,
    validate_download_save_path,
)
from app.application.history import DownloadHistorySnapshot
from app.domain.classification.validation import validate_classification_category_path
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.schemas.types import (
    MediaSource,
    MediaType,
    SystemConfigKey,
)

_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class SourceOrganizationHistoryPort(Protocol):
    """源路径变更专用的历史事务端口，复用历史快照而不扩展通用 CRUD 合同。"""

    def get_by_task(self, download_hash: str, downloader: str) -> DownloadHistorySnapshot | None:
        """按下载器与 Hash 查询历史，禁止跨实例复用同 Hash 记录。"""
        ...

    def save_source_operation(self, history: DownloadHistorySnapshot, operation: dict[str, Any]) -> bool:
        """CAS 保存检查点；确认计划同时写入 outbox，完成时事务同步下载路径。"""
        ...


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
    """源资源按 MB 主类型归档；艺术家合集是 MP 的明确扩展类型。"""
    if _source_value(getattr(media, "music_type", None)) == "artist":
        return "Artist Collection", []
    primary = str(getattr(media, "album_type", None) or "").strip()
    if not primary:
        primary = str(getattr(media, "category", None) or "").split("/")[0].strip()
    secondary = [
        str(item).strip()
        for item in (getattr(media, "secondary_types", None) or [])
        if str(item).strip() and str(item).strip() != primary
    ]
    main = primary.split("/")[0].strip()
    categories = {name.casefold(): name for name in ("Album", "Single", "EP", "Broadcast", "Other")}
    category = categories.get(main.casefold())
    if category is None and _source_value(getattr(media, "music_type", None)) == "recording":
        # 单曲 recording 本身没有 release-group 类型，使用本地单曲归档约定。
        category = "Single"
    return category or "", secondary


def _artist_name(media: Any) -> str:
    """仅从识别结果取艺人，不把未识别的种子标题当作艺人。"""
    value = getattr(media, "album_artist", None) or getattr(media, "artist", None)
    if not value and _source_value(getattr(media, "music_type", None)) == "artist":
        value = getattr(media, "name", None) or getattr(media, "title", None)
    return _safe_relative_name(value, label="艺人名称")


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
    source = request.media_source or (MediaSource.MusicBrainz if is_music else history_source)
    media_id = request.media_id
    if (
        not media_id
        and source
        and _source_value(source) == _source_value(history_source)
        and (not is_music or _source_value(request.music_type) == _source_value(getattr(history, "music_type", None)))
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
        raise ValueError("无法识别媒体信息，请搜索并指定正确的媒体 ID 后重新预览；未修改下载任务")
    return metainfo, media


def _download_root(current: PurePath, media: Any) -> tuple[Any, PurePath]:
    """和新下载共用目录匹配及分类拼装，不另行推导类别层级。"""
    helper = DirectoryHelper()
    directory = helper.get_download_dir_by_task_path(media, current.as_posix())
    if directory is None:
        directory = helper.get_dir(media, include_unsorted=True)
    if not directory or directory.storage != "local" or not directory.download_path:
        raise ValueError("没有找到匹配识别结果的本地资源目录")
    root = _local_path(directory.download_path, label="资源目录")
    if getattr(media, "type", None) == MediaType.MUSIC:
        category, _ = _normalize_music_category(media)
        if not category:
            raise ValueError("MusicBrainz 未返回主类别，请指定正确的专辑或单曲；不会归入未分类")
        return directory, root / category / _artist_name(media)
    target = build_media_download_path(Path(root.as_posix()), directory, media, directory_helper=helper)
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
    omit_year = False
    if getattr(media, "type", None) == MediaType.MUSIC:
        if str(getattr(media, "music_type", None) or "").casefold() == "artist":
            omit_year = True
            artist = getattr(media, "name", None) or getattr(media, "title", None)
            base = f"{_safe_relative_name(artist, label='艺人名称')} - 艺术家合集"
        else:
            omit_year = _source_value(getattr(media, "music_type", None)) == "recording"
            title = getattr(media, "title", None) if omit_year else (
                getattr(media, "album", None) or getattr(media, "title", None)
            )
            artist = getattr(media, "artist", None) if omit_year else (
                getattr(media, "album_artist", None) or getattr(media, "artist", None)
            )
            if not str(title or "").strip():
                raise ValueError("识别结果缺少作品名称，请指定正确的媒体 ID")
            base = " - ".join(str(part).strip() for part in (artist, title) if str(part or "").strip())
    else:
        base = str(getattr(media, "title", None) or "").strip()
    year = str(getattr(media, "year", None) or "").strip()
    if year and not omit_year and f"({year})" not in base:
        base = f"{base} ({year})"
    return _safe_relative_name(base, label="规范目录名")


def _downloader_kind(name: str) -> str | None:
    """返回已配置下载器类型，用于在预览阶段显式限定重命名能力。"""
    downloaders = get_configured_system_config().get(SystemConfigKey.Downloaders) or []
    match = next((item for item in downloaders if item.get("name") == name), None)
    return str(match.get("type") or "").casefold() if match else None


def _qb_rename_target(
    chain: Any,
    downloader: str,
    hash_value: str,
    current: PurePath,
    content: PurePath | None,
) -> tuple[str | None, str | None]:
    """通过 qB 文件清单区分独立顶层目录与单文件，拒绝多根任务。

    文件夹名称允许包含点号；不能使用 ``Path.suffix`` 猜测它是不是文件。
    qB 与 MP 的挂载路径可能不同，因此不能仅依据 MP 本地文件类型判断。
    """
    if _downloader_kind(downloader) != "qbittorrent" or not content:
        return None, None
    if not content.is_absolute() or not content.is_relative_to(current):
        return None, None
    relative_content = content.relative_to(current)
    if len(relative_content.parts) != 1:
        return None, None

    try:
        torrent_files = chain.torrent_files(tid=hash_value, downloader=downloader)
    except Exception:
        torrent_files = None
    file_paths = [
        PurePosixPath(str(getattr(item, "name", "")).replace("\\", "/"))
        for item in (torrent_files or [])
    ]
    if not file_paths or any(path.is_absolute() or ".." in path.parts or not path.parts for path in file_paths):
        return None, None
    if len(file_paths) == 1 and file_paths[0].parts == (relative_content.name,):
        return relative_content.name, "file"
    if not all(len(path.parts) >= 2 for path in file_paths):
        return None, None
    top_levels = {path.parts[0] for path in file_paths}
    return (relative_content.name, "folder") if top_levels == {relative_content.name} else (None, None)


def _task(chain: Any, hash_value: str, downloader: str | None) -> Any:
    """每次核验均读取指定下载器实时任务，避免缓存和同 Hash 跨实例串用。"""
    matches = [
        item for item in (chain.list_torrents(
            hashs=[hash_value], downloader=downloader, include_all_tags=True,
        ) or [])
        if str(item.hash or "").casefold() == hash_value.casefold()
        and (not downloader or item.downloader == downloader)
    ]
    if len(matches) != 1:
        raise ValueError("无法唯一定位下载任务，请检查下载器实例及任务是否存在")
    return matches[0]


def _files(chain: Any, downloader: str, hash_value: str) -> list[dict[str, Any]]:
    """冻结种子文件身份与路径，不读取或修改音频内容。"""
    items = chain.torrent_files(tid=hash_value, downloader=downloader)
    if not items:
        raise ValueError("下载器尚未返回文件清单，请等待种子元数据就绪")
    result = []
    for item in items:
        name = str(item.name or "").replace("\\", "/")
        path = PurePosixPath(name)
        if not name or path.is_absolute() or ".." in path.parts:
            raise ValueError("下载器返回不安全的文件路径")
        result.append({"id": item.id, "name": name, "size": item.size})
    return sorted(result, key=lambda item: item["id"])


def _operation_result(operation: dict[str, Any]) -> dict[str, Any]:
    """仅核验完成才对客户端报告已执行成功。"""
    complete = operation["state"] == "complete"
    plan = operation["plan"]
    return {
        **plan, "operation_id": operation["id"], "state": operation["state"],
        "message": operation.get("message"), "executed": complete,
        "renamed": complete and plan["rename_required"],
        "relocated": complete and plan["current_save_path"] != plan["target_save_path"],
    }


def reconcile_source_operation(
    hash_value: str, downloader: str, chain: Any, operation_id: str | None = None,
) -> dict[str, Any]:
    """恢复已确认计划；每个外部动作先持久化意图，不盲重试不确定请求。

    状态查询是显式 POST，可推进此前授权的移动。CAS 只有一个胜者能发出动作；
    请求超时或进程退出后只核验，不重新发出同一个动作。
    """
    repository: SourceOrganizationHistoryPort = chain.download_history_repository
    for _ in range(4):
        history = repository.get_by_task(hash_value, downloader)
        if history is None or not isinstance(history.note, dict):
            raise ValueError("未找到对应的资源规范化操作")
        operation = json.loads(json.dumps(history.note.get("source_organization", {})))
        if not operation or (operation_id and operation["id"] != operation_id):
            raise ValueError("未找到对应的资源规范化操作")
        if operation["state"] == "complete":
            return _operation_result(operation)
        plan = operation["plan"]
        torrent = _task(chain, hash_value, downloader)
        actual_files = _files(chain, downloader, hash_value)
        current = _local_path(torrent.save_path, label="当前保存路径").as_posix()
        content = _local_path(torrent.content_path or torrent.path, label="当前内容路径").as_posix()
        original = operation["original_files"]
        target_files = operation["target_files"]
        old_content = plan["current_content_path"]
        intermediate = (_local_path(plan["current_save_path"], label="原保存路径") / plan["proposed_root_name"]).as_posix()
        initial = (
            current == plan["current_save_path"]
            and content == old_content
            and actual_files == original
        )
        at_renamed_root = (
            current == plan["current_save_path"]
            and content == intermediate
            and actual_files == target_files
        )
        at_target = (
            current == plan["target_save_path"]
            and content == plan["target_content_path"]
            and actual_files == target_files
        )
        raw_state = str(getattr(torrent, "raw_state", "") or "").casefold()
        busy = not raw_state or raw_state == "moving" or "checking" in raw_state
        if busy:
            operation["message"] = "qB 正在移动、校验或尚未返回状态，等待稳定后同步 MP"
            return _operation_result(operation)
        invalid = (
            current not in (plan["current_save_path"], plan["target_save_path"])
            or content not in (old_content, intermediate, plan["target_content_path"])
            or actual_files not in (original, target_files)
        )
        if invalid or raw_state in ("error", "missingfiles", "unknown"):
            operation.update(state="needs_attention", message="下载器路径、文件清单或状态与确认计划不一致；已停止自动修改，请核对 qB")
            repository.save_source_operation(history, operation)
            return _operation_result(operation)
        if at_target and not busy:
            operation.update(state="complete", message="qB 内容路径及文件清单已核验，MP 下载历史和文件记录已同步")
            if repository.save_source_operation(history, operation):
                return _operation_result(operation)
            continue
        if operation["state"] == "needs_attention":
            recovered_state = (
                "prepared"
                if initial
                else "rename_requested"
                if at_renamed_root
                else None
            )
            if recovered_state:
                operation.update(
                    state=recovered_state,
                    updated_at=time.time(),
                    message="qB 已回到确认计划中的可验证阶段，继续安全核验",
                )
                if repository.save_source_operation(history, operation):
                    continue
                continue
        action = None
        if operation["state"] == "prepared" and initial and not busy:
            action = "rename" if plan["rename_required"] else "move"
        elif operation["state"] == "rename_requested" and at_renamed_root and not busy:
            if current != plan["target_save_path"]:
                action = "move"
        if action:
            operation.update(
                state=f"{action}_requested", updated_at=time.time(),
                message="已提交给 qB，等待实际路径核验；尚未报告成功",
            )
            if not repository.save_source_operation(history, operation):
                continue
            try:
                if action == "rename":
                    accepted = chain.run_module(
                        "rename_source_root", downloader=downloader, hash_string=hash_value,
                        old_name=plan["current_root_name"], new_name=plan["proposed_root_name"],
                        kind=plan["rename_kind"],
                    )
                else:
                    accepted = (chain.update_torrent(
                        hash_string=hash_value, downloader=downloader, save_path=plan["target_save_path"],
                    ) or {}).get("save_path")
                if not accepted:
                    raise RuntimeError("下载器未确认接收")
            except Exception:
                # 不重发、不逆向改名：请求可能已生效，后续仅按实时状态核验。
                operation["message"] = "请求结果未知，请稍后核验；不会自动重复改名或回滚"
                return _operation_result(operation)
            continue
        if time.time() - operation["updated_at"] > 3600 and not busy:
            operation.update(state="needs_attention", message="超过核验期限仍未到达目标路径，请检查 qB；不会重复提交动作")
            repository.save_source_operation(history, operation)
        elif operation["state"] != "needs_attention":
            operation["message"] = "qB 路径与文件清单正在收敛到确认计划，稍后将继续核验"
        return _operation_result(operation)
    return _operation_result(operation)


def organize_existing_source(hash_value: str, request: Any, chain: Any, media_chain: Any) -> dict[str, Any]:
    """生成可重放的预览计划，确认后仅通过下载器修改任务路径。"""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", hash_value):
        raise ValueError("hash 格式无效")
    torrent = _task(chain, hash_value, request.downloader)
    downloader = torrent.downloader
    if not downloader or _downloader_kind(downloader) != "qbittorrent":
        raise ValueError("资源规范化目前仅支持 qBittorrent")
    history = chain.download_history_repository.get_by_task(hash_value, downloader)
    if history is None:
        raise ValueError("未找到该下载器任务的下载历史，不能安全同步 MP 记录")
    previous = (history.note or {}).get("source_organization")
    if previous and previous.get("state") != "complete":
        return _operation_result(previous)
    current = _local_path(torrent.save_path, label="下载器返回的保存路径", validate=True)

    media = None
    category = None
    secondary_categories: list[str] = []
    if request.mode == "recognize" or request.smart_rename:
        _, media = _resolve_media(request, history, torrent, media_chain)
        override = None
        if request.media_category:
            if media.type == MediaType.MUSIC:
                if request.media_category.strip() != _normalize_music_category(media)[0]:
                    raise ValueError("音乐源资源按 MusicBrainz 主类别归档，不能用展示分类覆盖")
            else:
                override = _requested_category(request.media_category, media.type)
        media = source_directory_media(media, override)
        media_type = media.type.value
        if request.media_category:
            category = request.media_category.strip() if media.type == MediaType.MUSIC else override
            if media.type == MediaType.MUSIC:
                _, secondary_categories = _normalize_music_category(media)
        elif request.mode != "recognize":
            # 仅改名不依赖分类策略；识别到了名称但没有类别时仍然可用。
            category = None
        elif media.type == MediaType.MUSIC:
            category, secondary_categories = _normalize_music_category(media)
        else:
            category = _safe_relative_name(
                getattr(media, "category", None) or history.media_category,
                label="媒体类别",
            )
    else:
        media_type = request.type_name or history.type

    if request.mode == "keep":
        target = current
    elif request.mode == "manual":
        target = _manual_target(str(request.target_path))
    else:
        if not category:
            raise ValueError("识别结果缺少可用的媒体类别")
        _, target = _download_root(current, media)
        target = _local_path(target.as_posix(), label="目标保存路径", validate=True)

    content_text = str(torrent.content_path or torrent.path or "").strip()
    content = _local_path(content_text, label="下载器返回的内容路径") if content_text else None
    source_path = getattr(request, "source_path", None)
    if source_path and content != _local_path(source_path, label="源路径"):
        raise ValueError("源路径已变化，请重新选择下载任务根目录")
    current_root_name, rename_kind = _qb_rename_target(chain, downloader, hash_value, current, content)
    rename_supported = current_root_name is not None
    proposed_root_name = _root_name(media) if request.smart_rename and media else current_root_name
    if request.smart_rename and proposed_root_name and media and media.type == MediaType.MUSIC and request.mode == "recognize":
        artist_prefix = _artist_name(media) + " - "
        if proposed_root_name.startswith(artist_prefix):
            proposed_root_name = proposed_root_name[len(artist_prefix):]
        if _source_value(getattr(media, "music_type", None)) == "artist":
            years = re.search(r"(19\d{2}|20\d{2})\s*[-–~]\s*(19\d{2}|20\d{2})", torrent.title or "")
            if years:
                proposed_root_name += f" ({years[1]}-{years[2]})"
    if request.smart_rename and rename_kind == "file" and current_root_name and proposed_root_name:
        proposed_root_name += PurePosixPath(current_root_name).suffix
    rename_required = bool(
        request.smart_rename
        and rename_supported
        and proposed_root_name
        and proposed_root_name != current_root_name
    )
    if request.smart_rename and not rename_supported:
        if _downloader_kind(downloader) != "qbittorrent":
            raise ValueError("智能根目录重命名目前仅支持 qBittorrent")
        raise ValueError("无法确认任务拥有单一根目录或单文件；散列文件、多根任务不能安全重命名")

    target_text = target.as_posix()
    target_content = (target / proposed_root_name).as_posix() if proposed_root_name else None
    changed = current.as_posix() != target_text or rename_required
    rename_intermediate = (current / proposed_root_name).as_posix() if rename_required and proposed_root_name else None
    if changed and any(Path(path).exists() for path in (target_content, rename_intermediate) if path):
        raise ValueError("目标名称已存在，为避免覆盖或合并，请先检查目标目录")
    for other in chain.list_torrents(include_all_tags=True) or []:
        if other.downloader == downloader and str(other.hash).casefold() == hash_value.casefold():
            continue
        other_path = _local_path(other.content_path or other.path, label="其它任务路径")
        candidates = [content, _local_path(target_content, label="目标内容路径")]
        if any(candidate and (other_path.is_relative_to(candidate) or candidate.is_relative_to(other_path)) for candidate in candidates):
            raise ValueError("当前或目标目录与其它下载任务重叠，不能安全移动或改名")
    if request.execute and changed:
        expected = (
            request.expected_current_path == current.as_posix()
            and request.expected_target_path == target_text
            and request.expected_content_path == (content.as_posix() if content else None)
            and request.expected_root_name == proposed_root_name
        )
        if not expected:
            raise ValueError("任务路径或识别计划已变化，请重新预览后确认")

    media_source = getattr(media, "media_source", None)
    plan = {
        "hash": hash_value,
        "downloader": downloader,
        "mode": request.mode,
        "recognized": media is not None,
        "media_type": getattr(getattr(media, "type", None), "value", media_type),
        "media_source": getattr(media_source, "value", media_source),
        "media_id": str(getattr(media, "media_id", None) or "") or None,
        "title": getattr(media, "title", None) or getattr(media, "album", None),
        "year": str(getattr(media, "year", None) or "") or None,
        "current_save_path": current.as_posix(),
        "target_save_path": target_text,
        "current_content_path": content.as_posix() if content else None,
        "target_content_path": target_content,
        "rename_kind": rename_kind,
        "category": category,
        "secondary_categories": secondary_categories,
        "current_root_name": current_root_name,
        "proposed_root_name": proposed_root_name,
        "rename_supported": rename_supported,
        "rename_required": rename_required,
        "changed": changed,
        "executed": False,
        "relocated": False,
        "renamed": False,
    }

    if not request.execute:
        return plan
    return _begin_source_operation(hash_value, downloader, history, plan, chain)


def _begin_source_operation(hash_value: str, downloader: str, history: Any, plan: dict[str, Any], chain: Any) -> dict[str, Any]:
    """保存确认计划及精确路径映射；外部操作只能发生在 durable intent 提交之后。"""
    current = _local_path(plan["current_save_path"], label="原保存目录")
    target = _local_path(plan["target_save_path"], label="目标保存目录")
    current_root_name = plan["current_root_name"]
    proposed_root_name = plan["proposed_root_name"]
    rename_supported = plan["rename_supported"]
    rename_kind = plan["rename_kind"]
    target_content = plan["target_content_path"]
    target_text = plan["target_save_path"]
    original_files = _files(chain, downloader, hash_value)
    if not current_root_name or not proposed_root_name or not rename_supported:
        raise ValueError("无法确认唯一的任务根目录，不能同步下载历史")
    target_files = [
        {**item, "name": str(PurePosixPath(proposed_root_name).joinpath(*PurePosixPath(item["name"]).parts[1:]))}
        for item in original_files
    ]
    history_root = None
    try:
        candidate = _local_path(history.path, label="下载历史路径")
        if candidate.name == current_root_name:
            history_root = candidate
    except ValueError:
        pass
    file_updates: list[dict[str, Any]] = []
    for before, after in zip(original_files, target_files):
        # 文件记录既可能以保存目录为基准，也可能以任务根目录为基准；
        # fullpath 精确匹配是唯一更新条件，不对前缀相同的其它文件做替换。
        new_full = (target / after["name"]).as_posix()
        relative = PurePosixPath(after["name"])
        update = {
            "fullpath": new_full,
            "savepath": target_content if rename_kind == "folder" else target_text,
            "filepath": PurePosixPath(*relative.parts[1:]).as_posix() if rename_kind == "folder" else relative.name,
        }
        old_fullpaths = [(current / before["name"]).as_posix()]
        if history_root:
            remainder = PurePosixPath(before["name"]).parts[1:]
            history_fullpath = history_root.joinpath(*remainder).as_posix()
            if history_fullpath not in old_fullpaths:
                old_fullpaths.append(history_fullpath)
        file_updates.extend({**update, "old_fullpath": old_fullpath} for old_fullpath in old_fullpaths)
    operation: dict[str, Any] = {
        "id": uuid4().hex, "state": "prepared", "updated_at": time.time(),
        "plan": plan, "original_files": original_files, "target_files": target_files,
        "file_updates": file_updates,
    }
    if not chain.download_history_repository.save_source_operation(history, operation):
        raise ValueError("另一项规范化操作已更新此任务，请重新预览")
    return reconcile_source_operation(hash_value, downloader, chain, operation["id"])


def organize_source_path(request: Any, chain: Any, media_chain: Any) -> dict[str, Any]:
    """按 MP 映射后的内容路径定位任务，再复用历史入口的预览和执行。

    每次执行重新定位；不把合集内部的子目录提升到整个任务，不在匹配失败时
    退回文件系统改名，以免破坏做种或误改多个共享目录的任务。
    """
    source = _local_path(request.source_path, label="源路径", validate=True)
    matches = []
    for torrent in chain.list_torrents(include_all_tags=True) or []:
        content = str(torrent.content_path or torrent.path or "").strip()
        if not content:
            continue
        try:
            candidate = _local_path(content, label="下载器内容路径")
        except ValueError:
            continue
        if candidate == source:
            matches.append(torrent)
    if len(matches) != 1:
        raise ValueError("源路径未唯一对应下载任务根目录或单文件；请在下载历史中选择任务，不能直接修改做种文件")
    torrent = matches[0]
    if request.execute and request.expected_content_path != source.as_posix():
        raise ValueError("源路径或预览已变化，请重新预览")
    payload = request.model_copy(update={"downloader": torrent.downloader})
    return organize_existing_source(torrent.hash, payload, chain, media_chain)


def normalize_added_source(hash_value: str, downloader: str, chain: Any, media_chain: Any) -> dict[str, Any]:
    """持久下载后处理与手动入口复用同一计划；分类开关只决定是否同时归档。"""
    from app.schemas.download import DownloadSourceClassificationRequest

    history = chain.download_history_repository.get_by_task(hash_value, downloader)
    if not history:
        raise ValueError("下载历史尚未就绪")
    previous = (history.note or {}).get("source_organization")
    if previous:
        return reconcile_source_operation(hash_value, downloader, chain, previous["id"])
    torrent = _task(chain, hash_value, downloader)
    request = DownloadSourceClassificationRequest(
        downloader=downloader, mode="keep", type_name=history.type,
        music_type=history.music_type, smart_rename=True,
    )
    _, media = _resolve_media(request, history, torrent, media_chain)
    helper = DirectoryHelper()
    directory = helper.get_download_dir_by_task_path(media, str(torrent.save_path)) or helper.get_dir(media, include_unsorted=True)
    if directory and directory.download_category_folder:
        request.mode = "recognize"
    plan = organize_existing_source(hash_value, request, chain, media_chain)
    request = request.model_copy(update={
        "execute": True,
        "expected_current_path": plan["current_save_path"],
        "expected_target_path": plan["target_save_path"],
        "expected_content_path": plan["current_content_path"],
        "expected_root_name": plan["proposed_root_name"],
    })
    return organize_existing_source(hash_value, request, chain, media_chain)
