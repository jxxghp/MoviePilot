"""为已入库媒体提供受控的本地文件、STRM 与外挂字幕流。"""

import base64
import hashlib
import ipaddress
import json
import mimetypes
import re
import socket
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.adapters.network.http import RequestUtils
from app.runtime.settings import get_runtime_setting
from app.schemas.mediaserver import MediaServerSubtitleTrack as _SchemaMediaServerSubtitleTrack

_STREAM_CHUNK_SIZE = 256 * 1024
_AUDIO_STREAM_CHUNK_SIZE = 64 * 1024
_STRM_MAX_BYTES = 16 * 1024
_STREAM_MAX_REDIRECTS = 5
_FILE_TICKET_PREFIX = "file."
_FILE_TICKET_TTL_SECONDS = 6 * 60 * 60
_VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp", ".asf", ".avi", ".divx", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
        ".mpeg", ".mpg", ".mts", ".mxf", ".ogv", ".ts", ".vob", ".webm", ".wmv",
    }
)
_AUDIO_EXTENSIONS = frozenset(
    {
        ".aac", ".ac3", ".aif", ".aifc", ".aiff", ".alac", ".amr", ".ape", ".caf", ".dff",
        ".dsf", ".flac", ".m4a", ".m4b", ".m4p", ".mid", ".midi", ".mka", ".mp3", ".mpc",
        ".oga", ".ogg", ".opus", ".pcm", ".tak", ".tta", ".wav", ".wma", ".wv",
    }
)
_AUDIO_MIME_TYPES = {
    ".aac": "audio/aac",
    ".ac3": "audio/ac3",
    ".aif": "audio/aiff",
    ".aifc": "audio/aiff",
    ".aiff": "audio/aiff",
    ".alac": "audio/mp4",
    ".amr": "audio/amr",
    ".ape": "audio/x-ape",
    ".caf": "audio/x-caf",
    ".dff": "audio/x-dff",
    ".dsf": "audio/x-dsf",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".m4p": "audio/mp4",
    ".mid": "audio/midi",
    ".midi": "audio/midi",
    ".mka": "audio/x-matroska",
    ".mp3": "audio/mpeg",
    ".mpc": "audio/x-musepack",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".pcm": "audio/L16",
    ".tak": "audio/x-tak",
    ".tta": "audio/x-tta",
    ".wav": "audio/wav",
    ".wma": "audio/x-ms-wma",
    ".wv": "audio/x-wavpack",
}
_SUBTITLE_EXTENSIONS = frozenset({".ass", ".smi", ".srt", ".ssa", ".sub", ".sup", ".vtt"})
_SUBTITLE_MIME_TYPES = {
    ".ass": "text/x-ssa",
    ".smi": "application/smil",
    ".srt": "application/x-subrip",
    ".ssa": "text/x-ssa",
    ".sub": "application/octet-stream",
    ".sup": "application/octet-stream",
    ".vtt": "text/vtt",
    ".lrc": "text/plain; charset=utf-8",
}


def _file_ticket_cipher() -> Fernet:
    """
    从资源签名密钥派生文件播放票据密钥，避免把路径写入播放 URL。

    :return: 兼容 Fernet 的当前实例密钥
    """
    secret = str(get_runtime_setting("RESOURCE_SECRET_KEY"))
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def _create_file_stream_ticket(path: Path, user_id: int, media_type: str = "video") -> str:
    """
    创建带用途、加密路径和过期时间的短期单文件播放标识。

    :param path: 已通过文件管理权限检查的本地媒体或 STRM 文件
    :param user_id: 签发票据的当前用户主键
    :param media_type: 原生播放器应使用的媒体类型
    :return: 不暴露服务器路径的 URL 安全票据标识
    :raises HTTPException: 路径超出安全票据长度或无法解析时
    """
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="媒体文件不可读取") from None
    if not resolved.is_file() or len(str(resolved).encode("utf-8")) > 2048:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="媒体文件路径无效")
    try:
        file_stat = resolved.stat()
    except OSError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="媒体文件不可读取") from None
    payload = json.dumps(
        {
            "purpose": "media-file",
            "media_type": media_type,
            "path": str(resolved),
            "user_id": user_id,
            "device": file_stat.st_dev,
            "inode": file_stat.st_ino,
            "size": file_stat.st_size,
            "modified_ns": file_stat.st_mtime_ns,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    token = _file_ticket_cipher().encrypt(payload).decode("ascii").rstrip("=")
    return f"{_FILE_TICKET_PREFIX}{token}"


def _resolve_file_stream_ticket(item_id: str, user_id: Optional[int]) -> Path:
    """
    验证票据用途和有效期后还原当前可读取的单个本地文件。

    :param item_id: 已签发的 `file.` 媒体条目标识
    :param user_id: 当前已认证用户主键
    :return: 经重新解析且仍存在的单文件路径
    :raises HTTPException: 票据格式错误、过期或文件已不可读取时
    """
    token = item_id.removeprefix(_FILE_TICKET_PREFIX)
    if not re.fullmatch(r"[A-Za-z0-9_-]{64,4096}", token):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到已入库媒体")
    token += "=" * (-len(token) % 4)
    try:
        payload = json.loads(_file_ticket_cipher().decrypt(token.encode("ascii"), ttl=_FILE_TICKET_TTL_SECONDS))
        if (
            not isinstance(payload, dict)
            or payload.get("purpose") != "media-file"
            or not isinstance(payload.get("path"), str)
            or payload.get("user_id") != user_id
        ):
            raise InvalidToken
        path = Path(payload["path"]).resolve(strict=True)
        file_stat = path.stat()
    except (InvalidToken, OSError, RuntimeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到已入库媒体") from None
    if (
        not path.is_file()
        or payload.get("device") != file_stat.st_dev
        or payload.get("inode") != file_stat.st_ino
        or payload.get("size") != file_stat.st_size
        or payload.get("modified_ns") != file_stat.st_mtime_ns
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到已入库媒体")
    return path


def _file_stream_ticket_media_type(item_id: str) -> str:
    """
    读取已认证单文件票据中用于播放器呈现的音视频类型。

    :param item_id: 已通过 `_resolve_file_stream_ticket` 校验的票据
    :return: `audio` 或兼容旧票据的 `video`
    """
    token = item_id.removeprefix(_FILE_TICKET_PREFIX)
    token += "=" * (-len(token) % 4)
    try:
        payload = json.loads(_file_ticket_cipher().decrypt(token.encode("ascii")))
    except (InvalidToken, ValueError, json.JSONDecodeError):
        return "video"
    return "audio" if isinstance(payload, dict) and payload.get("media_type") == "audio" else "video"


def _is_file_stream_ticket(item_id: str) -> bool:
    """
    判断媒体条目标识是否使用受控的单文件票据格式。

    :param item_id: 前端请求的媒体条目标识
    :return: 以固定票据前缀开头时返回 True
    """
    return item_id.startswith(_FILE_TICKET_PREFIX)


def _resolve_media_path(path: Optional[str]) -> Path:
    """
    验证媒体服务器报告的路径是当前进程可读取的本地文件。

    :param path: 媒体服务器返回的文件路径
    :return: 已验证的文件路径
    :raises HTTPException: 路径缺失或本机无法访问时
    """
    if not path:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="媒体服务器未提供文件路径")
    media_path = Path(path)
    if not media_path.is_file():
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="当前实例无法访问媒体文件")
    return media_path


def _configured_stream_hosts(mediaservers: Any, storages: Any) -> set[str]:
    """
    从调用方提供的媒体服务器和云存储配置中收集主机名。

    :param mediaservers: 应用层已读取的媒体服务器配置
    :param storages: 应用层已读取的存储配置
    :return: 可作为 STRM 私有网络目标的主机名集合
    """
    hosts: set[str] = set()
    host_keys = {"api_url", "base_url", "endpoint", "host", "server", "url"}

    def add_host(value: Any) -> None:
        """
        从配置地址中提取规范化主机名。

        :param value: 配置中的地址值
        """
        if not isinstance(value, str) or not value.strip():
            return
        candidate = value.strip()
        if "://" not in candidate:
            candidate = f"http://{candidate}"
        hostname = urlsplit(candidate).hostname
        if hostname:
            hosts.add(hostname.casefold().rstrip("."))

    def visit(value: Any, key: str = "") -> None:
        """
        遍历存储配置中地址字段，避免把令牌或其他文本作为主机。

        :param value: 待检查的配置值
        :param key: 当前字段名称
        """
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key).casefold())
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, key)
        elif key in host_keys:
            add_host(value)

    for config in mediaservers:
        visit(config.config or {})
    for config in storages:
        visit(config.config or {})
    return hosts


def _validate_stream_url(url: str, *, configured_hosts: set[str]) -> str:
    """
    校验 STRM 上游地址，只允许 HTTP(S) 且禁止未配置的私有网络目标。

    :param url: STRM 文件中的原始地址
    :param configured_hosts: 管理员明确配置的主机名集合
    :return: 通过校验的原地址
    :raises HTTPException: 地址协议、凭据或目标主机不受支持时
    """
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="STRM 播放地址不受支持")

    if hostname in configured_hosts:
        return url
    try:
        resolved = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except (OSError, ValueError):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="STRM 播放地址无法解析") from None
    if not resolved:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="STRM 播放地址无法解析")
    for entry in resolved:
        try:
            address = ipaddress.ip_address(entry[4][0])
        except ValueError:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="STRM 播放地址无法解析") from None
        if not address.is_global:
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="STRM 播放地址目标不受支持")
    return url


def _sanitize_strm_direct_url(raw_url: str) -> Optional[str]:
    """
    规范化并校验可供客户端直连播放的 STRM HTTP(S) 地址，剔除内嵌凭据与播放器附加头后缀。

    本函数不发起 DNS 或网络请求，仅用于向已认证客户端返回安全的 HTTP(S) 直链文本；
    服务端代理转发仍须走 ``_validate_stream_url``。

    :param raw_url: STRM 文件或媒体服务器报告的原始地址
    :return: 规范化后的 HTTP(S) 直链；非 HTTP(S)、包含账号密码或控制字符时返回 None
    """
    candidate = raw_url.strip().split("|", 1)[0].strip()
    if not candidate or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        return None
    parsed = urlsplit(candidate)
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").strip().rstrip(".")
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    safe_path = quote(parsed.path, safe="/:@!$&'()*+,;=-._~%")
    safe_query = quote(parsed.query, safe="/?:@!$&'()*+,;=-._~%")
    return urlunsplit((scheme, parsed.netloc, safe_path, safe_query, ""))


def _extract_strm_target_line(content: str) -> Optional[str]:
    """
    从 STRM 文本中提取唯一有效的媒体地址行，忽略空行与 ``#`` 注释指令。

    :param content: STRM 文本内容
    :return: 唯一的目标行文本；为空或包含多条媒体地址时返回 None
    """
    lines = [
        stripped
        for raw_line in content.splitlines()
        if (stripped := raw_line.strip()) and not stripped.startswith("#")
    ]
    if len(lines) != 1:
        return None
    return lines[0]


def _extract_local_strm_direct_url(path: Path) -> Optional[str]:
    """
    从本机可读的 STRM 文件中提取供客户端直连播放的 HTTP(S) 地址。

    当 STRM 指向本地文件路径、体积超限或编码无效时不抛出异常，仅返回 None，
    交由常规流校验分支返回对应状态码。

    :param path: 本机 STRM 文件路径
    :return: 无内嵌凭据的 HTTP(S) 直链地址；不可直连时返回 None
    """
    if path.suffix.casefold() != ".strm":
        return None
    try:
        if path.stat().st_size > _STRM_MAX_BYTES:
            return None
        content = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return None
    target = _extract_strm_target_line(content)
    return _sanitize_strm_direct_url(target) if target else None


def _resolve_strm_source(path: Path, configured_hosts: set[str]) -> tuple[str, Path | str]:
    """
    解析单行 STRM 文件为本地媒体文件或经过校验的 HTTP(S) 源。

    :param path: STRM 文件路径
    :param configured_hosts: 管理员明确配置的媒体服务器与存储主机名
    :return: 来源类型及本地文件路径或远端 URL
    :raises HTTPException: STRM 无效、过大或目标不受支持时
    """
    try:
        if path.stat().st_size > _STRM_MAX_BYTES:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="STRM 文件格式不受支持")
        content = path.read_text(encoding="utf-8-sig").strip()
    except UnicodeError:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="STRM 文件格式不受支持"
        ) from None
    except OSError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="STRM 文件不可读取") from None

    target = _extract_strm_target_line(content)
    if not target:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="STRM 文件内容无效")
    source = target.split("|", 1)[0].strip()
    parsed = urlsplit(source)
    if parsed.scheme:
        normalized = _sanitize_strm_direct_url(source) or source
        return "remote", _validate_stream_url(normalized, configured_hosts=configured_hosts)

    source_path = Path(source)
    if not source_path.is_absolute():
        source_path = path.parent / source_path
    if not source_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="STRM 指向的媒体文件不存在")
    return "file", source_path


def _stream_source(path: Path, configured_hosts: set[str]) -> tuple[str, Path | str]:
    """
    识别普通媒体文件或 STRM 并解析实际播放来源。

    :param path: 媒体服务器条目报告的路径
    :param configured_hosts: 管理员明确配置的媒体服务器与存储主机名
    :return: 来源类型及本地文件路径或远端 URL
    """
    return _resolve_strm_source(path, configured_hosts) if path.suffix.casefold() == ".strm" else ("file", path)


def _is_matching_subtitle(video_path: Path, subtitle_path: Path) -> bool:
    """
    判断字幕是否与视频使用相同的基础文件名。

    :param video_path: 视频或 STRM 文件路径
    :param subtitle_path: 候选字幕文件路径
    :return: 字幕名与视频名完全相同或仅附加语言/标记时返回 True
    """
    video_stem = video_path.stem.casefold()
    subtitle_stem = subtitle_path.stem.casefold()
    if subtitle_stem == video_stem:
        return True
    if not subtitle_stem.startswith(video_stem):
        return False
    suffix = subtitle_stem[len(video_stem) :]
    if not suffix or suffix[0] not in "._-":
        return False
    parts = re.split(r"[._-]+", suffix[1:])
    flags = {"cc", "forced", "hi", "sdh"}
    return bool(
        parts
        and (re.fullmatch(r"[a-z]{2,3}", parts[0]) or parts[0] in flags)
        and all(re.fullmatch(r"[a-z0-9]{2,8}", part) for part in parts[1:])
    )


def _subtitle_tracks(
    item_id: str,
    video_path: Path,
    request: Request,
    server: Optional[str] = None,
) -> list[_SchemaMediaServerSubtitleTrack]:
    """
    枚举视频同目录中名称匹配的常见外挂字幕。

    :param item_id: 媒体服务器条目标识
    :param video_path: 视频或 STRM 文件路径
    :param request: 用于生成当前实例字幕流地址的请求
    :param server: 可选媒体服务器配置名，避免同一条目标识跨服务器冲突
    :return: 已校验的外挂字幕清单
    """
    try:
        candidates = sorted(video_path.parent.iterdir(), key=lambda candidate: candidate.name.casefold())
    except OSError:
        return []
    tracks = []
    for candidate in candidates:
        if (
            candidate.suffix.casefold() not in _SUBTITLE_EXTENSIONS
            or not candidate.is_file()
            or not _is_sidecar_in_media_directory(video_path, candidate)
            or not _is_matching_subtitle(video_path, candidate)
        ):
            continue
        suffix = candidate.stem[len(video_path.stem) :].strip("._-")
        language_match = re.match(r"[a-z]{2,3}(?:[-_][a-z0-9]{2,8})?", suffix, re.IGNORECASE)
        query = {"itemid": item_id, "subtitle_id": candidate.name}
        if server:
            query["server"] = server
        subtitle_url = request.url_for("stream_item_subtitle").include_query_params(**query)
        tracks.append(
            _SchemaMediaServerSubtitleTrack(
                subtitle_id=candidate.name,
                title=suffix.replace(".", " ").replace("_", " ").replace("-", " ") or "字幕",
                language=language_match.group(0) if language_match else None,
                url=str(subtitle_url),
            )
        )
    return tracks


def _is_sidecar_in_media_directory(video_path: Path, candidate: Path) -> bool:
    """
    限制外挂字幕实际路径位于视频目录内，阻止目录外的符号链接越界。

    :param video_path: 视频或 STRM 文件路径
    :param candidate: 候选字幕文件路径
    :return: 规范化后的字幕路径仍位于媒体目录内时返回 True
    """
    try:
        return candidate.resolve(strict=True).parent == video_path.parent.resolve()
    except OSError:
        return False


def _find_subtitle_path(video_path: Path, subtitle_id: str) -> Path:
    """
    确认字幕标识是当前视频同目录内的匹配文件名。

    :param video_path: 视频或 STRM 文件路径
    :param subtitle_id: 客户端请求的字幕文件名
    :return: 已验证的字幕文件路径
    :raises HTTPException: 字幕路径越界、类型不受支持或名称不匹配时返回 404
    """
    if Path(subtitle_id).name != subtitle_id or "\\" in subtitle_id or "/" in subtitle_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到外挂字幕")
    subtitle_path = video_path.parent / subtitle_id
    if (
        subtitle_path.suffix.casefold() not in _SUBTITLE_EXTENSIONS
        or not subtitle_path.is_file()
        or not _is_sidecar_in_media_directory(video_path, subtitle_path)
        or not _is_matching_subtitle(video_path, subtitle_path)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到外挂字幕")
    return subtitle_path


def _find_lyrics_path(audio_path: Path) -> Optional[Path]:
    """
    查找音频同目录中仅文件主名相同的 LRC 歌词。

    :param audio_path: 音频文件或 STRM 文件路径
    :return: 位于媒体目录内的同名 LRC 路径；不存在或目录外链接时返回 None
    """
    try:
        candidates = sorted(audio_path.parent.iterdir(), key=lambda candidate: candidate.name.casefold())
    except OSError:
        return None
    for candidate in candidates:
        if (
            candidate.suffix.casefold() == ".lrc"
            and candidate.stem.casefold() == audio_path.stem.casefold()
            and candidate.is_file()
            and _is_sidecar_in_media_directory(audio_path, candidate)
        ):
            return candidate
    return None


def _is_audio_path(path: Path) -> bool:
    """
    根据媒体条目的扩展名判断其是否属于受支持的本地音频格式。

    :param path: 媒体或 STRM 文件路径
    :return: 扩展名属于音频集合时返回 True
    """
    return path.suffix.casefold() in _AUDIO_EXTENSIONS


def _parse_range_header(range_header: Optional[str], size: int) -> tuple[int, int, int]:
    """
    将单个 HTTP 字节范围转换为起点、终点和响应状态。

    :param range_header: 客户端 Range 请求头
    :param size: 文件总字节数
    :return: 起始字节、结束字节和 HTTP 状态码
    :raises HTTPException: 范围格式错误或请求超出文件长度时返回 416
    """
    if not range_header:
        return 0, size - 1, status.HTTP_200_OK
    if "," in range_header or not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header.strip(), re.IGNORECASE):
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="请求的字节范围无效",
            headers={"Content-Range": f"bytes */{size}"},
        )
    first, last = range_header.strip()[6:].split("-", 1)
    if not first:
        suffix_length = int(last)
        if suffix_length <= 0 or size == 0:
            raise HTTPException(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                detail="请求的字节范围无效",
                headers={"Content-Range": f"bytes */{size}"},
            )
        start = max(0, size - suffix_length)
        end = size - 1
    else:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if start >= size or end < start:
            raise HTTPException(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                detail="请求的字节范围超出文件长度",
                headers={"Content-Range": f"bytes */{size}"},
            )
    return start, end, status.HTTP_206_PARTIAL_CONTENT


def _file_stream_response(
    path: Path,
    range_header: Optional[str],
    *,
    head_only: bool = False,
) -> StreamingResponse:
    """
    以有界内存和 HTTP Range 语义读取本地媒体或字幕文件。

    - 对音频显式返回标准 IANA MIME 类型并采用较小分片（64KB），便于移动端播放器在无后缀流 URL 下立即边加载边播放；
    - 支持 `HEAD` 元数据探测请求，仅返回长度、MIME 与 Range 头而不读取文件内容。

    :param path: 已由媒体服务器条目或同名字幕发现的本地文件
    :param range_header: 客户端 Range 请求头
    :param head_only: 为 True 时只返回响应头而不输出正文
    :return: 带正确长度及 Content-Range 的流式响应
    """
    try:
        file = path.open("rb")
        size = path.stat().st_size
    except OSError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="媒体文件不可读取") from None
    try:
        start, end, response_status = _parse_range_header(range_header, size)
    except HTTPException:
        file.close()
        raise
    length = max(0, end - start + 1)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Length": str(length),
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(path.name, safe='')}",
    }
    if response_status == status.HTTP_206_PARTIAL_CONTENT:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    suffix = path.suffix.casefold()
    content_type = (
        _SUBTITLE_MIME_TYPES.get(suffix)
        or _AUDIO_MIME_TYPES.get(suffix)
        or mimetypes.guess_type(path.name)[0]
    )
    chunk_size = _AUDIO_STREAM_CHUNK_SIZE if suffix in _AUDIO_EXTENSIONS else _STREAM_CHUNK_SIZE

    if head_only:
        file.close()
        return StreamingResponse(
            iter(()),
            status_code=response_status,
            media_type=content_type or "application/octet-stream",
            headers=headers,
        )

    def chunks() -> Iterator[bytes]:
        """
        从所选范围读取固定大小数据块并在客户端断开时关闭文件。

        :yields: 文件内容块
        """
        remaining = length
        try:
            file.seek(start)
            while remaining > 0:
                chunk = file.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            file.close()

    return StreamingResponse(
        chunks(),
        status_code=response_status,
        media_type=content_type or "application/octet-stream",
        headers=headers,
    )


def _validate_range_syntax(range_header: Optional[str]) -> None:
    """
    在请求远端 STRM 来源前拒绝多段和格式不正确的字节范围。

    :param range_header: 客户端 Range 请求头
    :raises HTTPException: 格式不支持时返回 416
    """
    if range_header and (
        "," in range_header or not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header.strip(), re.IGNORECASE)
    ):
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="请求的字节范围无效",
        )


def _stream_origin(url: str) -> tuple[str, str, int]:
    """
    提取 HTTP(S) URL 的规范化源站三元组，用于控制跨源重定向时剥离上游会话头。

    :param url: 待提取源站的地址
    :return: `(scheme, hostname, port)` 三元组
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, hostname, port


def _open_upstream_stream(
    url: str,
    range_header: Optional[str],
    configured_hosts: set[str],
    extra_headers: Optional[dict[str, str]] = None,
) -> tuple[Any, AbstractContextManager[Any], RequestUtils]:
    """
    打开媒体服务器或 STRM 上游流，并逐跳验证重定向地址。

    仅在与初始上游完全同源的请求上附带 `extra_headers`，跨源重定向到网盘 CDN 等外部目标时自动剥离。

    :param url: 媒体服务器原始流或已解析的 STRM 地址
    :param range_header: 客户端 Range 请求头
    :param configured_hosts: 管理员明确配置的媒体服务器与存储主机名
    :param extra_headers: 可选仅发往同源媒体服务器的会话关联请求头
    :return: 上游响应、负责关闭响应的上下文和 HTTP 客户端
    :raises HTTPException: 上游不可用、目标不安全或重定向次数超限时
    """
    client = RequestUtils(timeout=30, verify=True)
    current_url = url
    initial_origin = _stream_origin(url)
    try:
        for _ in range(_STREAM_MAX_REDIRECTS + 1):
            _validate_stream_url(current_url, configured_hosts=configured_hosts)
            headers = {"Accept-Encoding": "identity"}
            if extra_headers and _stream_origin(current_url) == initial_origin:
                headers.update(extra_headers)
            if range_header:
                headers["Range"] = range_header
            context = client.get_stream(
                current_url,
                headers=headers,
                allow_redirects=False,
                timeout=(10, 60),
                raise_exception=True,
            )
            try:
                response = context.__enter__()
            except Exception:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="STRM 上游连接失败") from None
            if response is None:
                context.__exit__(None, None, None)
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="STRM 上游连接失败")
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                context.__exit__(None, None, None)
                if not location:
                    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="STRM 上游重定向无效")
                current_url = urljoin(current_url, location)
                continue
            return response, context, client
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="STRM 上游重定向次数过多")
    except Exception:
        client.close()
        raise


def _remote_stream_response(
    url: str,
    range_header: Optional[str],
    configured_hosts: set[str],
    extra_headers: Optional[dict[str, str]] = None,
    *,
    head_only: bool = False,
) -> StreamingResponse:
    """
    代理媒体服务器或 STRM 上游流，并保留其真实的 Range 响应。

    :param url: 已解析的上游媒体地址
    :param range_header: 客户端 Range 请求头
    :param configured_hosts: 管理员明确配置的媒体服务器与存储主机名
    :param extra_headers: 可选仅发往源媒体服务器同源主机的会话关联请求头
    :return: 流式响应
    :raises HTTPException: 上游错误或未实现所请求的 Range 时
    """
    _validate_range_syntax(range_header)
    upstream, context, client = _open_upstream_stream(
        url, range_header, configured_hosts, extra_headers=extra_headers
    )
    if upstream.status_code == status.HTTP_416_RANGE_NOT_SATISFIABLE:
        headers = {key: upstream.headers[key] for key in ("Content-Range", "Accept-Ranges") if key in upstream.headers}
        context.__exit__(None, None, None)
        client.close()
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="请求的字节范围无效",
            headers=headers,
        )
    if upstream.status_code not in {status.HTTP_200_OK, status.HTTP_206_PARTIAL_CONTENT}:
        context.__exit__(None, None, None)
        client.close()
        mapped_status = {
            status.HTTP_404_NOT_FOUND: status.HTTP_404_NOT_FOUND,
            status.HTTP_405_METHOD_NOT_ALLOWED: status.HTTP_501_NOT_IMPLEMENTED,
            status.HTTP_501_NOT_IMPLEMENTED: status.HTTP_501_NOT_IMPLEMENTED,
        }.get(upstream.status_code, status.HTTP_502_BAD_GATEWAY)
        raise HTTPException(status_code=mapped_status, detail="STRM 上游媒体读取失败")
    content_type = upstream.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
    if content_type in {"text/html", "application/xhtml+xml", "application/json"}:
        context.__exit__(None, None, None)
        client.close()
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="上游未返回媒体文件流")
    if range_header and upstream.status_code != status.HTTP_206_PARTIAL_CONTENT:
        context.__exit__(None, None, None)
        client.close()
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="STRM 上游不支持 Range，无法提供可靠跳转",
        )
    response_headers = {
        key: upstream.headers[key]
        for key in ("Accept-Ranges", "Content-Length", "Content-Range", "ETag", "Last-Modified")
        if key in upstream.headers
    }
    response_headers["Cache-Control"] = "private, no-store"
    media_type = upstream.headers.get("Content-Type", "application/octet-stream")
    upstream_status = upstream.status_code

    if head_only:
        context.__exit__(None, None, None)
        client.close()
        return StreamingResponse(
            iter(()),
            status_code=upstream_status,
            media_type=media_type,
            headers=response_headers,
        )

    def chunks() -> Iterator[bytes]:
        """
        转发上游数据块并在客户端中断时释放 HTTP 连接。

        :yields: 上游媒体内容块
        """
        try:
            for chunk in upstream.iter_content(chunk_size=_STREAM_CHUNK_SIZE):
                if chunk:
                    yield chunk
        finally:
            context.__exit__(None, None, None)
            client.close()

    return StreamingResponse(
        chunks(),
        status_code=upstream_status,
        media_type=media_type,
        headers=response_headers,
    )
