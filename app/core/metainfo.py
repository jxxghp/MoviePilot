from pathlib import Path
from functools import lru_cache
from typing import Tuple, List, Optional

import regex as re

from app.core.config import settings
from app.core.meta import MetaAnime, MetaVideo, MetaBase
from app.core.meta.infopath import (
    clear_parsed_title_for_parent_merge,
    should_use_parent_title_for_file_stem,
)
from app.core.meta.words import WordsMatcher
from app.log import logger
from app.schemas.types import MediaType
from app.utils import rust_accel


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


def _empty_metainfo() -> dict:
    """
    返回媒体标签的默认结构，避免不同识别请求之间共享可变状态。
    """
    return {
        'tmdbid': None,
        'doubanid': None,
        'type': None,
        'episode_group': None,
        'begin_season': None,
        'end_season': None,
        'total_season': None,
        'begin_episode': None,
        'end_episode': None,
        'total_episode': None,
    }


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


def _find_metainfo_python(title: str) -> Tuple[str, dict]:
    """
    使用 Python 解析标题中的显式媒体标签，作为 Rust 入口不可用时的兜底。
    """
    metainfo = _empty_metainfo()
    if not title:
        return title, metainfo
    # 从标题中提取媒体信息 格式为{[tmdbid=xxx;type=xxx;g=xxx;s=xxx;e=xxx]}
    results = _BRACED_METAINFO_RE.findall(title)
    if results:
        for result in results:
            # 查找tmdbid信息
            tmdbid = _BRACED_TMDBID_RE.search(result)
            if tmdbid and tmdbid.group(0).isdigit():
                metainfo['tmdbid'] = tmdbid.group(0)
            # 查找豆瓣id信息
            doubanid = _BRACED_DOUBANID_RE.search(result)
            if doubanid and doubanid.group(0).isdigit():
                metainfo['doubanid'] = doubanid.group(0)
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
            if tmdbid or mtype or episode_group or begin_season or end_season or begin_episode or end_episode:
                title = title.replace(f"{{[{result}]}}", '')

    # 支持Emby格式的ID标签；第一个 [tmdbid] 历史上始终优先处理，用于覆盖前面 {[...]} 中的旧标签。
    tmdb_match = _EMBY_TMDB_RE_LIST[0].search(title)
    if tmdb_match:
        metainfo['tmdbid'] = tmdb_match.group(1)
        title = _EMBY_TMDB_RE_LIST[0].sub('', title).strip()
    elif not metainfo['tmdbid']:
        # 保持原有优先级：[tmdbid] > [tmdb] > {tmdbid} > {tmdb}
        for tmdb_re in _EMBY_TMDB_RE_LIST[1:]:
            tmdb_match = tmdb_re.search(title)
            if tmdb_match:
                metainfo['tmdbid'] = tmdb_match.group(1)
                title = tmdb_re.sub('', title).strip()
                break

    # 计算季集总数
    _apply_range_total(metainfo, 'begin_season', 'end_season', 'total_season')
    _apply_range_total(metainfo, 'begin_episode', 'end_episode', 'total_episode')
    return title, metainfo


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
    media_exts = settings.RMT_MEDIAEXT + settings.RMT_SUBEXT + settings.RMT_AUDIOEXT
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
    if metainfo.get('tmdbid'):
        try:
            meta.tmdbid = int(metainfo['tmdbid'])
        except ValueError as _:
            logger.warn("tmdbid 必须是数字")
    if metainfo.get('doubanid'):
        meta.doubanid = metainfo['doubanid']
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
    from app.core.meta.customization import CustomizationMatcher
    from app.core.meta.releasegroup import ReleaseGroupsMatcher
    from app.core.meta.streamingplatform import StreamingPlatforms
    from app.db.systemconfig_oper import SystemConfigOper
    from app.schemas.types import SystemConfigKey

    systemconfig = SystemConfigOper()
    custom_release_groups = systemconfig.get(SystemConfigKey.CustomReleaseGroups)
    if isinstance(custom_release_groups, list):
        custom_release_groups = list(filter(None, custom_release_groups))
    release_matcher = ReleaseGroupsMatcher()
    release_groups = release_matcher._ReleaseGroupsMatcher__release_groups
    if custom_release_groups:
        release_groups = f"{release_groups}|{'|'.join(custom_release_groups)}"

    customization = CustomizationMatcher._normalize_customization(
        systemconfig.get(SystemConfigKey.Customization)
    )
    return {
        "custom_words": systemconfig.get(SystemConfigKey.CustomIdentifiers) or [],
        "media_exts": settings.RMT_MEDIAEXT + settings.RMT_SUBEXT + settings.RMT_AUDIOEXT,
        "release_groups": release_groups,
        "customization": customization,
        "streaming_platforms": StreamingPlatforms()._lookup_cache,
    }


@lru_cache(maxsize=256)
def _rust_custom_parse_options(custom_words: Tuple[str, ...]) -> dict:
    """
    缓存带自定义识别词的 Rust Meta 配置，避免同一组识别词重复构造配置对象。
    """
    options = dict(_rust_default_parse_options())
    options["custom_words"] = list(custom_words)
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
        "tmdbid": parsed.get("tmdbid"),
        "doubanid": parsed.get("doubanid"),
        "episode_group": parsed.get("episode_group"),
        "fps": parsed.get("fps"),
    }
    for key, value in fields.items():
        setattr(meta, key, value)
    return meta


def MetaInfo(title: str, subtitle: Optional[str] = None, custom_words: List[str] = None) -> MetaBase:
    """
    根据标题和副标题识别元数据
    :param title: 标题、种子名、文件名
    :param subtitle: 副标题、描述
    :param custom_words: 自定义识别词列表
    :return: MetaAnime、MetaVideo
    """
    rust_meta = _meta_from_rust(
        rust_accel.parse_metainfo(title, subtitle, _rust_parse_options(custom_words))
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


def MetaInfoPath(path: Path, custom_words: List[str] = None) -> MetaBase:
    """
    根据路径识别元数据
    :param path: 路径
    :param custom_words: 自定义识别词列表
    """
    rust_meta = _meta_from_rust(
        rust_accel.parse_metainfo_path(str(path), _rust_parse_options(custom_words))
    )
    if rust_meta:
        return rust_meta
    # 文件元数据，不包含后缀
    file_meta = MetaInfo(title=path.name, custom_words=custom_words)
    if should_use_parent_title_for_file_stem(path.stem, path.parent.name, file_meta):
        clear_parsed_title_for_parent_merge(file_meta)
    # 上级目录元数据
    dir_meta = MetaInfo(title=path.parent.name, custom_words=custom_words)
    if file_meta.type == MediaType.TV or dir_meta.type != MediaType.TV:
        # 合并元数据
        file_meta.merge(dir_meta)
    # 上上级目录元数据
    root_meta = MetaInfo(title=path.parent.parent.name, custom_words=custom_words)
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
    rust_result = rust_accel.find_metainfo(title)
    if rust_result:
        return rust_result["title"], rust_result["metainfo"]
    return _find_metainfo_python(title)
