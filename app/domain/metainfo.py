import hashlib
import logging
from pathlib import Path
from functools import lru_cache
from typing import Tuple, List, Optional

import regex as re

from app.domain.meta.metaanime import MetaAnime
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.meta.metavideo import MetaVideo
from app.domain.meta.infopath import (
    clear_parsed_title_for_parent_merge,
    should_use_parent_title_for_file_stem,
)
from app.domain.meta.parsepipeline import enhance_meta
from app.domain.meta.words import WordsMatcher, get_custom_words
from app.domain.meta.customization import CustomizationMatcher, get_customization
from app.domain.meta.releasegroup import ReleaseGroupsMatcher
from app.domain.meta.runtime import (
    get_audio_extensions,
    get_media_extensions,
    get_metainfo_accelerator,
)
from app.schemas.types import MediaSource, MediaType
from app.schemas.media import normalize_media_source, resolve_media_identity


_ANIME_BRACKET_RE = re.compile(r'【[+0-9XVPI-]+】\s*【', re.IGNORECASE)
_ANIME_DASH_EPISODE_RE = re.compile(r'\s+-\s+[\dv]{1,4}\s+', re.IGNORECASE)
_VIDEO_SEASON_EPISODE_RE = re.compile(
    r"S\d{2}\s*-\s*S\d{2}|S\d{2}|\s+S\d{1,2}|"
    r"EP?\d{2,4}\s*-\s*EP?\d{2,4}|EP?\d{2,4}|\s+EP?\d{1,4}",
    re.IGNORECASE,
)
_ANIME_SQUARE_BRACKET_RE = re.compile(r'\[[+0-9XVPI-]+]\s*\[', re.IGNORECASE)

_BRACED_METAINFO_RE = re.compile(r'(?<={\[)[\W\w]+(?=]})')
_BRACED_TMDBID_RE = re.compile(r'(?<=tmdbid=)\d+')
_BRACED_DOUBANID_RE = re.compile(r'(?<=doubanid=)\d+')
_BRACED_BANGUMIID_RE = re.compile(r'(?<=bangumiid=)\d+')
_BRACED_ANILISTID_RE = re.compile(r'(?<=anilistid=)\d+')
_BRACED_TYPE_RE = re.compile(r'(?<=type=)\w+')
_BRACED_EPISODE_GROUP_RE = re.compile(r'(?:^|;)g=([0-9a-fA-F]+)(?=;|$)')
_BRACED_BEGIN_SEASON_RE = re.compile(r'(?<=s=)\d+')
_BRACED_END_SEASON_RE = re.compile(r'(?<=s=\d+-)\d+')
_BRACED_BEGIN_EPISODE_RE = re.compile(r'(?<=e=)\d+')
_BRACED_END_EPISODE_RE = re.compile(r'(?<=e=\d+-)\d+')
_EMBY_TMDB_RE_LIST = (
    re.compile(r'\[tmdbid[=\-](\d+)\]'),
    re.compile(r'\[tmdb[=\-](\d+)\]'),
    re.compile(r'\{tmdbid[=\-](\d+)\}'),
    re.compile(r'\{tmdb[=\-](\d+)\}'),
)
_EXTENDED_MEDIA_ID_RE_LIST = {
    "bangumi": (
        re.compile(r'\[bangumiid[=\-](\d+)\]'),
        re.compile(r'\[bangumi[=\-](\d+)\]'),
        re.compile(r'\{bangumiid[=\-](\d+)\}'),
        re.compile(r'\{bangumi[=\-](\d+)\}'),
    ),
    "anilist": (
        re.compile(r'\[anilistid[=\-](\d+)\]'),
        re.compile(r'\[anilist[=\-](\d+)\]'),
        re.compile(r'\{anilistid[=\-](\d+)\}'),
        re.compile(r'\{anilist[=\-](\d+)\}'),
    ),
}
_EXTENDED_MEDIA_ID_TAG_RE = re.compile(
    r'(?:bangumi(?:id)?|anilist(?:id)?)[=\-]\d+',
    re.IGNORECASE,
)
_GENERIC_MEDIA_ID_TAG_RE = re.compile(r'(?:^|[;\[])media_(?:source|id)=', re.IGNORECASE)
_RUST_PARSE_OPTIONS_CACHE_KEY = "_cache_key"
logger = logging.getLogger(__name__)

_LEGACY_BRACED_ID_PATTERNS = (
    (MediaSource.TMDB, _BRACED_TMDBID_RE),
    (MediaSource.Douban, _BRACED_DOUBANID_RE),
    (MediaSource.Bangumi, _BRACED_BANGUMIID_RE),
    (MediaSource.AniList, _BRACED_ANILISTID_RE),
)
_LEGACY_ID_KEYS = (
    (MediaSource.TMDB, "tmdbid"),
    (MediaSource.Douban, "doubanid"),
    (MediaSource.Bangumi, "bangumiid"),
    (MediaSource.AniList, "anilistid"),
)


def _empty_metainfo() -> dict:
    """
    返回媒体标签的默认结构，避免不同识别请求之间共享可变状态。
    """
    return {
        'media_source': None,
        'media_id': None,
        'type': None,
        'episode_group': None,
        'begin_season': None,
        'end_season': None,
        'total_season': None,
        'begin_episode': None,
        'end_episode': None,
        'total_episode': None,
    }


def _normalize_metainfo_identity(metainfo: dict) -> dict:
    """
    将解析器输出归一为唯一媒体身份，并移除历史来源专用字段。

    Rust扩展或旧缓存仍可能返回专用字段，因此兼容只保留在这一输入边界。
    """
    normalized = dict(metainfo or {})
    media_source, media_id = resolve_media_identity(media=normalized)
    if not media_source:
        for source, key in _LEGACY_ID_KEYS:
            value = normalized.get(key)
            normalized_id = str(value).strip() if value is not None else ""
            if normalized_id and normalized_id != "0":
                media_source, media_id = source, normalized_id
                break
    for _, key in _LEGACY_ID_KEYS:
        normalized.pop(key, None)
    normalized["media_source"] = media_source
    normalized["media_id"] = media_id
    return normalized


def _apply_range_total(metainfo: dict, begin_key: str, end_key: str, total_key: str) -> None:
    """
    计算季/集范围总数；保留原有倒序输入自动交换的兼容行为。
    """
    if metainfo.get(begin_key) and metainfo.get(end_key):
        if metainfo[begin_key] > metainfo[end_key]:
            metainfo[begin_key], metainfo[end_key] = metainfo[end_key], metainfo[begin_key]
        metainfo[total_key] = metainfo[end_key] - metainfo[begin_key] + 1
    elif metainfo.get(begin_key) and not metainfo.get(end_key):
        metainfo[total_key] = 1


def _rust_parse_options_cache_key(options: dict) -> str:
    """
    生成 Rust Meta 配置缓存键，避免扩展层每次重新展开大配置。
    """
    digest = hashlib.blake2b(digest_size=16)

    def update(value) -> None:
        digest.update(repr(value).encode("utf-8"))
        digest.update(b"\0")

    streaming_platforms = options.get("streaming_platforms") or {}
    update(tuple(options.get("custom_words") or []))
    update(tuple(options.get("media_exts") or []))
    update(options.get("release_groups") or "")
    update(tuple(options.get("customization") or []))
    update(tuple(sorted(
        (str(key), str(value))
        for key, value in streaming_platforms.items()
    )))
    return digest.hexdigest()


def _find_metainfo_python(title: str) -> Tuple[str, dict]:
    """
    使用 Python 解析标题中的显式媒体标签，作为 Rust 入口不可用时的兜底。
    """
    metainfo = _empty_metainfo()
    legacy_identities = {}
    if not title:
        return title, metainfo
    # 自定义识别词是面向用户的独立语法，继续使用各数据源专用 ID 字段。
    results = _BRACED_METAINFO_RE.findall(title)
    if results:
        for result in results:
            legacy_matches = []
            for source, pattern in _LEGACY_BRACED_ID_PATTERNS:
                legacy_match = pattern.search(result)
                if legacy_match:
                    legacy_matches.append(legacy_match)
                    normalized_id = legacy_match.group(0)
                    if normalized_id.isdigit() and normalized_id != "0":
                        legacy_identities[source] = normalized_id
            # 查找媒体类型
            mtype = _BRACED_TYPE_RE.search(result)
            if mtype:
                media_type = mtype.group(0)
                if media_type in ["movie", "movies"]:
                    metainfo['type'] = MediaType.MOVIE
                elif media_type == "tv":
                    metainfo['type'] = MediaType.TV
            # 查找剧集组
            episode_group = _BRACED_EPISODE_GROUP_RE.search(result)
            if episode_group:
                metainfo['episode_group'] = episode_group.group(1)
            # 查找季信息
            begin_season = _BRACED_BEGIN_SEASON_RE.search(result)
            if begin_season and begin_season.group(0).isdigit():
                metainfo['begin_season'] = int(begin_season.group(0))
            end_season = _BRACED_END_SEASON_RE.search(result)
            if end_season and end_season.group(0).isdigit():
                metainfo['end_season'] = int(end_season.group(0))
            # 查找集信息
            begin_episode = _BRACED_BEGIN_EPISODE_RE.search(result)
            if begin_episode and begin_episode.group(0).isdigit():
                metainfo['begin_episode'] = int(begin_episode.group(0))
            end_episode = _BRACED_END_EPISODE_RE.search(result)
            if end_episode and end_episode.group(0).isdigit():
                metainfo['end_episode'] = int(end_episode.group(0))
            # 去除title中该部分
            if (
                legacy_matches
                or mtype
                or episode_group
                or begin_season
                or end_season
                or begin_episode
                or end_episode
            ):
                title = title.replace(f"{{[{result}]}}", '')

    # 支持Emby格式的ID标签；第一个 [tmdbid] 历史上始终优先处理，用于覆盖前面 {[...]} 中的旧标签。
    tmdb_match = _EMBY_TMDB_RE_LIST[0].search(title)
    if tmdb_match:
        if tmdb_match.group(1) != "0":
            legacy_identities[MediaSource.TMDB] = tmdb_match.group(1)
        title = _EMBY_TMDB_RE_LIST[0].sub('', title).strip()
    elif MediaSource.TMDB not in legacy_identities:
        # 保持原有优先级：[tmdbid] > [tmdb] > {tmdbid} > {tmdb}
        for tmdb_re in _EMBY_TMDB_RE_LIST[1:]:
            tmdb_match = tmdb_re.search(title)
            if tmdb_match:
                if tmdb_match.group(1) != "0":
                    legacy_identities[MediaSource.TMDB] = tmdb_match.group(1)
                title = tmdb_re.sub('', title).strip()
                break

    for source_name, patterns in _EXTENDED_MEDIA_ID_RE_LIST.items():
        source = normalize_media_source(source_name)
        if not source or source in legacy_identities:
            continue
        for media_id_re in patterns:
            media_id_match = media_id_re.search(title)
            if not media_id_match:
                continue
            if media_id_match.group(1) != "0":
                legacy_identities[source] = media_id_match.group(1)
            title = media_id_re.sub('', title).strip()
            break

    media_source, media_id = None, None
    for source, _ in _LEGACY_ID_KEYS:
        if legacy_identities.get(source):
            media_source, media_id = source, legacy_identities[source]
            break
    metainfo['media_source'] = media_source
    metainfo['media_id'] = media_id

    # 计算季集总数
    _apply_range_total(metainfo, 'begin_season', 'end_season', 'total_season')
    _apply_range_total(metainfo, 'begin_episode', 'end_episode', 'total_episode')
    return title, _normalize_metainfo_identity(metainfo)


def _build_meta_info(
        title: str,
        subtitle: Optional[str] = None,
        custom_words: List[str] = None,
) -> MetaBase:
    """
    根据标题构造元数据
    """
    # 原标题
    org_title = title
    # 预处理标题
    title, apply_words = WordsMatcher().prepare(title, custom_words=custom_words)
    # 获取标题中媒体信息
    title, metainfo = find_metainfo(title)
    # 判断是否处理文件
    media_exts = get_media_extensions()
    title_path = Path(title) if title else None
    if title_path and title_path.suffix.lower() in media_exts:
        isfile = True
        # 去掉后缀
        title = title_path.stem
    else:
        isfile = False
    # 识别
    meta = MetaAnime(title, subtitle, isfile) if is_anime(title) else MetaVideo(title, subtitle, isfile)
    # 记录原标题
    meta.title = org_title
    # 记录使用的识别词
    meta.apply_words = apply_words or []
    # 修正媒体信息
    media_source, media_id = resolve_media_identity(media=metainfo)
    if media_source and media_id:
        meta.media_source = media_source
        meta.media_id = media_id
    if metainfo.get('type'):
        meta.type = MediaType(metainfo['type']) if isinstance(metainfo['type'], str) else metainfo['type']
    if metainfo.get('episode_group'):
        meta.episode_group = metainfo['episode_group']
    if metainfo.get('begin_season') is not None:
        meta.begin_season = metainfo['begin_season']
    if metainfo.get('end_season') is not None:
        meta.end_season = metainfo['end_season']
    if metainfo.get('total_season') is not None:
        meta.total_season = metainfo['total_season']
    if metainfo.get('begin_episode') is not None:
        meta.begin_episode = metainfo['begin_episode']
    if metainfo.get('end_episode') is not None:
        meta.end_episode = metainfo['end_episode']
    if metainfo.get('total_episode') is not None:
        meta.total_episode = metainfo['total_episode']
    return meta


@lru_cache(maxsize=1)
def _rust_default_parse_options() -> dict:
    """
    缓存 Rust Meta 默认解析配置，避免热路径反复读取配置并复制流媒体平台大表。
    """
    from app.domain.meta.streamingplatform import StreamingPlatforms

    release_groups = ReleaseGroupsMatcher().get_release_groups()

    customization = CustomizationMatcher.normalize_customization(
        get_customization()
    )
    options = {
        "custom_words": get_custom_words() or [],
        # PyO3 当前按 Python list 提取 Vec<String>，不能传 tuple。
        "media_exts": list(get_media_extensions()),
        "release_groups": release_groups,
        "customization": customization,
        "streaming_platforms": StreamingPlatforms().get_lookup_cache(),
    }
    options[_RUST_PARSE_OPTIONS_CACHE_KEY] = _rust_parse_options_cache_key(options)
    return options


@lru_cache(maxsize=256)
def _rust_custom_parse_options(custom_words: Tuple[str, ...]) -> dict:
    """
    缓存带自定义识别词的 Rust Meta 配置，避免同一组识别词重复构造配置对象。
    """
    options = dict(_rust_default_parse_options())
    options["custom_words"] = list(custom_words)
    options[_RUST_PARSE_OPTIONS_CACHE_KEY] = _rust_parse_options_cache_key(options)
    return options


def _rust_parse_options(custom_words: List[str] = None) -> dict:
    """
    收集 Rust Meta 解析所需的运行时配置，避免 Rust 层直接访问数据库和 settings。
    """
    if custom_words is None:
        return _rust_default_parse_options()
    return _rust_custom_parse_options(tuple(custom_words or []))


def clear_rust_parse_options_cache() -> None:
    """
    清理 Rust Meta 默认解析配置缓存，供系统配置变更后重载使用。
    """
    _rust_default_parse_options.cache_clear()
    _rust_custom_parse_options.cache_clear()


def _meta_from_rust(parsed: dict) -> Optional[MetaBase]:
    """
    将 Rust 解析结果灌回现有 MetaVideo/MetaAnime 对象，保留下游属性和方法兼容性。
    """
    if not parsed:
        return None
    parsed = _normalize_metainfo_identity(parsed)
    meta = MetaAnime("") if parsed.get("kind") == "anime" else MetaVideo("")
    type_map = {
        MediaType.MOVIE.value: MediaType.MOVIE,
        MediaType.TV.value: MediaType.TV,
        MediaType.COLLECTION.value: MediaType.COLLECTION,
        MediaType.UNKNOWN.value: MediaType.UNKNOWN,
    }
    fields = {
        "isfile": parsed.get("isfile") or False,
        "title": parsed.get("title") or "",
        "org_string": parsed.get("org_string"),
        "subtitle": parsed.get("subtitle"),
        "type": type_map.get(parsed.get("type"), MediaType.UNKNOWN),
        "cn_name": parsed.get("cn_name"),
        "en_name": parsed.get("en_name"),
        "original_name": parsed.get("original_name"),
        "year": parsed.get("year"),
        "total_season": parsed.get("total_season") or 0,
        "begin_season": parsed.get("begin_season"),
        "end_season": parsed.get("end_season"),
        "total_episode": parsed.get("total_episode") or 0,
        "begin_episode": parsed.get("begin_episode"),
        "end_episode": parsed.get("end_episode"),
        "part": parsed.get("part"),
        "resource_type": parsed.get("resource_type"),
        "resource_effect": parsed.get("resource_effect"),
        "resource_pix": parsed.get("resource_pix"),
        "resource_team": parsed.get("resource_team"),
        "customization": parsed.get("customization"),
        "web_source": parsed.get("web_source"),
        "video_encode": parsed.get("video_encode"),
        "video_bit": parsed.get("video_bit"),
        "audio_encode": parsed.get("audio_encode"),
        "apply_words": parsed.get("apply_words") or [],
        "media_source": parsed.get("media_source"),
        "media_id": parsed.get("media_id"),
        "episode_group": parsed.get("episode_group"),
        "fps": parsed.get("fps"),
    }
    for key, value in fields.items():
        setattr(meta, key, value)
    return meta


def _requires_python_metainfo(
    title: str,
    custom_words: Optional[List[str]] = None,
) -> bool:
    """
    判断标题或临时识别词是否必须由 Python 解析器处理媒体身份标签。

    :param title: 原始标题
    :param custom_words: 临时识别词
    :return: 是否必须使用Python解析器
    """
    candidates = [title or "", *(custom_words or [])]
    if any(_GENERIC_MEDIA_ID_TAG_RE.search(candidate) for candidate in candidates):
        return True
    contains_extended_id = any(
        _EXTENDED_MEDIA_ID_TAG_RE.search(candidate) for candidate in candidates
    )
    accelerator = get_metainfo_accelerator()
    return contains_extended_id and bool(
        accelerator and not accelerator.supports_extended_media_ids()
    )


def _builtin_meta_info(title: str, subtitle: Optional[str] = None, custom_words: List[str] = None,
                       force_video: bool = False) -> MetaBase:
    """
    按内建规则识别元数据，作为解析管道恒不弃权的第一环

    :param title: 标题、种子名、文件名
    :param subtitle: 副标题、描述
    :param custom_words: 自定义识别词列表
    :param force_video: 音频后缀的影视附加轨（如评论音轨）强制按视频解析，用于影视整理场景
    :return: MetaAnime、MetaVideo、MetaMusic
    """
    # 音频文件名直接走音乐分支，避免进入影视季集解析，但影视附加音轨强制走视频解析
    audio_suffix = Path(title).suffix.lower() if title else ""
    if not force_video and audio_suffix in get_audio_extensions():
        return MetaMusic(
            org_string=title,
            title=Path(title).stem,
            audio_format=audio_suffix.lstrip(".").upper() or None,
            parse_title=True,
        )
    rust_meta = None
    accelerator = get_metainfo_accelerator()
    if accelerator and not _requires_python_metainfo(title, custom_words):
        rust_meta = _meta_from_rust(
            accelerator.parse_metainfo(
                title,
                subtitle,
                _rust_parse_options(custom_words),
            )
        )
    if rust_meta:
        return rust_meta
    meta = _build_meta_info(title=title, subtitle=subtitle, custom_words=custom_words)
    if meta.apply_words:
        original_meta = _build_meta_info(title=title, subtitle=subtitle)
        meta.original_name = original_meta.name or meta.name
    else:
        meta.original_name = meta.name
    return meta


def MetaInfo(title: str, subtitle: Optional[str] = None, custom_words: List[str] = None,
             force_video: bool = False) -> MetaBase:
    """
    根据标题和副标题识别元数据
    :param title: 标题、种子名、文件名
    :param subtitle: 副标题、描述
    :param custom_words: 自定义识别词列表
    :param force_video: 音频后缀的影视附加轨（如评论音轨）强制按视频解析，用于影视整理场景
    :return: MetaAnime、MetaVideo、MetaMusic
    """
    meta = _builtin_meta_info(
        title=title, subtitle=subtitle, custom_words=custom_words, force_video=force_video
    )
    # 音乐元数据自带一套音频字段，不属于影视解析契约的槽位，不交给扩展环
    if isinstance(meta, MetaMusic):
        return meta
    return enhance_meta(meta, title=title, subtitle=subtitle, custom_words=custom_words)


def MetaInfoPath(path: Path, custom_words: List[str] = None, force_video: bool = False) -> MetaBase:
    """
    根据路径识别元数据
    :param path: 路径
    :param custom_words: 自定义识别词列表
    :param force_video: 音频后缀的影视附加轨（如评论音轨）强制按视频解析，用于影视整理场景
    """
    # 音频文件直接构造音乐元数据，不参与父目录季集合并，影视附加音轨强制走视频解析
    audio_suffix = path.suffix.lower()
    if not force_video and audio_suffix in get_audio_extensions():
        return MetaMusic(
            org_string=path.name,
            title=path.stem,
            audio_format=audio_suffix.lstrip(".").upper() or None,
        ).apply_path_context(path)
    path_context = " ".join(
        [path.name, path.parent.name, path.parent.parent.name]
    )
    rust_meta = None
    accelerator = get_metainfo_accelerator()
    if accelerator and not _requires_python_metainfo(path_context, custom_words):
        rust_meta = _meta_from_rust(
            accelerator.parse_metainfo_path(
                str(path),
                _rust_parse_options(custom_words),
            )
        )
    builtin_meta = rust_meta or _merged_path_meta(path, custom_words)
    # 扩展环按整条路径识别一次，此时各级目录的内建解析结果已合并完毕
    return enhance_meta(
        builtin_meta, title=path.name, custom_words=custom_words, path=str(path)
    )


def _merged_path_meta(path: Path, custom_words: List[str] = None) -> MetaBase:
    """
    按文件名与两级父目录逐层识别并合并元数据

    :param path: 路径
    :param custom_words: 自定义识别词列表
    :return: 合并后的元数据对象
    """
    # 文件元数据，不包含后缀
    file_meta = _builtin_meta_info(title=path.name, custom_words=custom_words)
    if should_use_parent_title_for_file_stem(path.stem, path.parent.name, file_meta):
        clear_parsed_title_for_parent_merge(file_meta)
    # 上级目录元数据
    dir_meta = _builtin_meta_info(title=path.parent.name, custom_words=custom_words)
    if file_meta.type == MediaType.TV or dir_meta.type != MediaType.TV:
        # 合并元数据
        file_meta.merge(dir_meta)
    # 上上级目录元数据
    root_meta = _builtin_meta_info(title=path.parent.parent.name, custom_words=custom_words)
    if file_meta.type == MediaType.TV or root_meta.type != MediaType.TV:
        # 合并元数据
        file_meta.merge(root_meta)
    return file_meta


def is_anime(name: str) -> bool:
    """
    判断是否为动漫
    :param name: 名称
    :return: 是否动漫
    """
    if not name:
        return False
    if _ANIME_BRACKET_RE.search(name):
        return True
    if _ANIME_DASH_EPISODE_RE.search(name):
        return True
    if _VIDEO_SEASON_EPISODE_RE.search(name):
        return False
    if _ANIME_SQUARE_BRACKET_RE.search(name):
        return True
    return False


def find_metainfo(title: str) -> Tuple[str, dict]:
    """
    从标题中提取媒体信息
    """
    rust_result = None
    accelerator = get_metainfo_accelerator()
    if accelerator and not _requires_python_metainfo(title):
        rust_result = accelerator.find_metainfo(title)
    if rust_result:
        return rust_result["title"], _normalize_metainfo_identity(rust_result["metainfo"])
    return _find_metainfo_python(title)
