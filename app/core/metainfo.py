from pathlib import Path
from typing import Tuple

import regex as re

from app.core.config import settings
from app.core.meta import MetaAnime, MetaVideo, MetaBase
from app.core.meta.words import WordsMatcher
from app.log import logger
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
    title, metainfo = find_metainfo(title)
    # 判断是否处理文件
    if title and Path(title).suffix.lower() in settings.RMT_MEDIAEXT:
        isfile = True
        # 去掉后缀
        title = Path(title).stem
    else:
        isfile = False
    # 识别
    meta = MetaAnime(title, subtitle, isfile) if is_anime(title) else MetaVideo(title, subtitle, isfile)
    # 记录原标题
    meta.title = org_title
    #  记录使用的识别词
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
        meta.type = metainfo['type']
    if metainfo.get('begin_season'):
        meta.begin_season = metainfo['begin_season']
    if metainfo.get('end_season'):
        meta.end_season = metainfo['end_season']
    if metainfo.get('total_season'):
        meta.total_season = metainfo['total_season']
    if metainfo.get('begin_episode'):
        meta.begin_episode = metainfo['begin_episode']
    if metainfo.get('end_episode'):
        meta.end_episode = metainfo['end_episode']
    if metainfo.get('total_episode'):
        meta.total_episode = metainfo['total_episode']
    return meta


def MetaInfoPath(path: Path) -> MetaBase:
    """
    根据路径识别元数据
    :param path: 路径
    """
    # 文件元数据，不包含后缀
    file_meta = MetaInfo(title=path.name)
    # 上级目录元数据
    dir_meta = MetaInfo(title=path.parent.name)
    # 合并元数据
    file_meta.merge(dir_meta)
    # 上上级目录元数据
    root_meta = MetaInfo(title=path.parent.parent.name)
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


def find_metainfo(title: str) -> Tuple[str, dict]:
    """
    从标题中提取媒体信息
    """
    metainfo = {
        'tmdbid': None,
        'doubanid': None,
        'type': None,
        'begin_season': None,
        'end_season': None,
        'total_season': None,
        'begin_episode': None,
        'end_episode': None,
        'total_episode': None,
    }
    if not title:
        return title, metainfo
    # 从标题中提取媒体信息 格式为{[tmdbid=xxx;type=xxx;s=xxx;e=xxx]}
    results = re.findall(r'(?<={\[)[\W\w]+(?=]})', title)
    if not results:
        return title, metainfo
    for result in results:
        # 查找tmdbid信息
        tmdbid = re.findall(r'(?<=tmdbid=)\d+', result)
        if tmdbid and tmdbid[0].isdigit():
            metainfo['tmdbid'] = tmdbid[0]
        # 查找豆瓣id信息
        doubanid = re.findall(r'(?<=doubanid=)\d+', result)
        if doubanid and doubanid[0].isdigit():
            metainfo['doubanid'] = doubanid[0]
        # 查找媒体类型
        mtype = re.findall(r'(?<=type=)\w+', result)
        if mtype:
            match mtype[0]:
                case "movie":
                    metainfo['type'] = MediaType.MOVIE
                case "tv":
                    metainfo['type'] = MediaType.TV
                case _:
                    pass
        # 查找季信息
        begin_season = re.findall(r'(?<=s=)\d+', result)
        if begin_season and begin_season[0].isdigit():
            metainfo['begin_season'] = int(begin_season[0])
        end_season = re.findall(r'(?<=s=\d+-)\d+', result)
        if end_season and end_season[0].isdigit():
            metainfo['end_season'] = int(end_season[0])
        # 查找集信息
        begin_episode = re.findall(r'(?<=e=)\d+', result)
        if begin_episode and begin_episode[0].isdigit():
            metainfo['begin_episode'] = int(begin_episode[0])
        end_episode = re.findall(r'(?<=e=\d+-)\d+', result)
        if end_episode and end_episode[0].isdigit():
            metainfo['end_episode'] = int(end_episode[0])
        # 去除title中该部分
        if tmdbid or mtype or begin_season or end_season or begin_episode or end_episode:
            title = title.replace(f"{{[{result}]}}", '')
    # 计算季集总数
    if metainfo.get('begin_season') and metainfo.get('end_season'):
        if metainfo['begin_season'] > metainfo['end_season']:
            metainfo['begin_season'], metainfo['end_season'] = metainfo['end_season'], metainfo['begin_season']
        metainfo['total_season'] = metainfo['end_season'] - metainfo['begin_season'] + 1
    elif metainfo.get('begin_season') and not metainfo.get('end_season'):
        metainfo['total_season'] = 1
    if metainfo.get('begin_episode') and metainfo.get('end_episode'):
        if metainfo['begin_episode'] > metainfo['end_episode']:
            metainfo['begin_episode'], metainfo['end_episode'] = metainfo['end_episode'], metainfo['begin_episode']
        metainfo['total_episode'] = metainfo['end_episode'] - metainfo['begin_episode'] + 1
    elif metainfo.get('begin_episode') and not metainfo.get('end_episode'):
        metainfo['total_episode'] = 1
    return title, metainfo
