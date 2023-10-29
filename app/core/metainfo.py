from pathlib import Path

import regex as re

from app.core.config import settings
from app.core.meta import MetaAnime, MetaVideo, MetaBase
from app.core.meta.words import WordsMatcher
from app.schemas.types import MediaType


def MetaInfo(title: str, subtitle: str = None) -> MetaBase:
    """
    根据标题和副标题识别元数据
    :param title: 标题、种子名、文件名
    :param subtitle: 副标题、描述
    :return: MetaAnime、MetaVideo
    """
    # 原标题
    org_title = title
    # 预处理标题
    title, apply_words = WordsMatcher().prepare(title)
    # 获取标题中媒体信息
    title, mediainfo = find_mediainfo(title)
    # 判断是否处理文件
    if title and Path(title).suffix.lower() in settings.RMT_MEDIAEXT:
        isfile = True
    else:
        isfile = False
    # 识别
    meta = MetaAnime(title, subtitle, isfile) if is_anime(title) else MetaVideo(title, subtitle, isfile)
    # 记录原标题
    meta.title = org_title
    #  记录使用的识别词
    meta.apply_words = apply_words or []
    # 修正媒体信息
    if mediainfo['tmdbid']:
        meta.tmdbid = mediainfo['tmdbid']
    if mediainfo['type']:
        meta.type = mediainfo['type']
    if mediainfo['begin_season']:
        meta.begin_season = mediainfo['begin_season']
    if mediainfo['end_season']:
        meta.end_season = mediainfo['end_season']
    if mediainfo['total_season']:
        meta.total_season = mediainfo['total_season']
    if mediainfo['begin_episode']:
        meta.begin_episode = mediainfo['begin_episode']
    if mediainfo['end_episode']:
        meta.end_episode = mediainfo['end_episode']
    if mediainfo['total_episode']:
        meta.total_episode = mediainfo['total_episode']
    return meta


def MetaInfoPath(path: Path) -> MetaBase:
    """
    根据路径识别元数据
    :param path: 路径
    """
    # 上级目录元数据
    dir_meta = MetaInfo(title=path.parent.name)
    # 文件元数据，不包含后缀
    file_meta = MetaInfo(title=path.stem)
    # 合并元数据
    file_meta.merge(dir_meta)
    return file_meta


def is_anime(name: str) -> bool:
    """
    判断是否为动漫
    :param name: 名称
    :return: 是否动漫
    """
    if not name:
        return False
    if re.search(r'【[+0-9XVPI-]+】\s*【', name, re.IGNORECASE):
        return True
    if re.search(r'\s+-\s+[\dv]{1,4}\s+', name, re.IGNORECASE):
        return True
    if re.search(r"S\d{2}\s*-\s*S\d{2}|S\d{2}|\s+S\d{1,2}|EP?\d{2,4}\s*-\s*EP?\d{2,4}|EP?\d{2,4}|\s+EP?\d{1,4}", name,
                 re.IGNORECASE):
        return False
    if re.search(r'\[[+0-9XVPI-]+]\s*\[', name, re.IGNORECASE):
        return True
    return False


def find_mediainfo(title: str) -> (str, dict):
    """
    从标题中提取媒体信息
    """
    media_info = {
        'tmdbid': None,
        'type': None,
        'begin_season': None,
        'end_season': None,
        'total_season': None,
        'begin_episode': None,
        'end_episode': None,
        'total_episode': None,
    }
    if not title:
        return title, media_info
    # 从标题中提取媒体信息 格式为{[tmdbid=xxx;type=xxx;s=xxx;e=xxx]}
    results = re.findall(r'(?<={\[)[\W\w]+(?=]})', title)
    if not results:
        return title, media_info
    for result in results:
        tmdbid = re.findall(r'(?<=tmdbid=)\d+', result)
        # 查找tmdbid信息
        if tmdbid and tmdbid[0].isdigit():
            media_info['tmdbid'] = tmdbid[0]
        # 查找媒体类型
        mtype = re.findall(r'(?<=type=)\d+', result)
        if mtype:
            match mtype[0]:
                case "movie":
                    media_info['type'] = MediaType.MOVIE
                case "tv":
                    media_info['type'] = MediaType.TV
                case _:
                    pass
        # 查找季信息
        begin_season = re.findall(r'(?<=s=)\d+', result)
        if begin_season and begin_season[0].isdigit():
            media_info['begin_season'] = int(begin_season[0])
        end_season = re.findall(r'(?<=s=\d+-)\d+', result)
        if end_season and end_season[0].isdigit():
            media_info['end_season'] = int(end_season[0])
        # 查找集信息
        begin_episode = re.findall(r'(?<=e=)\d+', result)
        if begin_episode and begin_episode[0].isdigit():
            media_info['begin_episode'] = int(begin_episode[0])
        end_episode = re.findall(r'(?<=e=\d+-)\d+', result)
        if end_episode and end_episode[0].isdigit():
            media_info['end_episode'] = int(end_episode[0])
        # 去除title中该部分
        if tmdbid or mtype or begin_season or end_season or begin_episode or end_episode:
            title = title.replace(f"{{[{result}]}}", '')
    # 计算季集总数
    if media_info['begin_season'] and media_info['end_season']:
        if media_info['begin_season'] > media_info['end_season']:
            media_info['begin_season'], media_info['end_season'] = media_info['end_season'], media_info['begin_season']
        media_info['total_season'] = media_info['end_season'] - media_info['begin_season'] + 1
    elif media_info['begin_season'] and not media_info['end_season']:
        media_info['total_season'] = 1
    if media_info['begin_episode'] and media_info['end_episode']:
        if media_info['begin_episode'] > media_info['end_episode']:
            media_info['begin_episode'], media_info['end_episode'] = media_info['end_episode'], media_info['begin_episode']
        media_info['total_episode'] = media_info['end_episode'] - media_info['begin_episode'] + 1
    elif media_info['begin_episode'] and not media_info['end_episode']:
        media_info['total_episode'] = 1
    return title, media_info
