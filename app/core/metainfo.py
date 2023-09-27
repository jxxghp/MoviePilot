from pathlib import Path

import regex as re

from app.core.config import settings
from app.core.meta import MetaAnime, MetaVideo, MetaBase
from app.core.meta.words import WordsMatcher


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
