import regex as re

from app.core.meta.metabase import MetaBase
from app.utils.string import StringUtils

AUXILIARY_CN_STEM_FULLMATCH_RE = re.compile(
    r"^(双语|字幕|特效|内封|外挂|官译|简体|繁体|繁中|简中|中英|简英|多语|"
    r"国英|台粤|音轨|评论|国配|台配|粤语|韩语|日语|杜比|全景声|无损|中字|"
    r"国语|原声)+$"
)


def should_use_parent_title_for_file_stem(
    stem: str, parent_dir_name: str, file_meta: MetaBase
) -> bool:
    """
    文件名（无后缀）是否仅为简繁体/字幕/特效等辅助说明，应改用父目录标题识别。
    要求：
    - stem 纯中文且能被辅助关键词完全覆盖（无残留有意义汉字）
    - 父目录含拉丁字母，避免纯中文资源目录误把正片中文名当标签清空
    """
    if not file_meta.isfile or not stem or not parent_dir_name:
        return False
    if file_meta.tmdbid or file_meta.doubanid:
        return False
    if not re.search(r"[A-Za-z]{2,}", parent_dir_name):
        return False
    if not StringUtils.is_all_chinese(stem):
        return False
    if len(stem) > 16:
        return False
    if not AUXILIARY_CN_STEM_FULLMATCH_RE.match(stem):
        return False
    if re.search(r"[第共]\s*[0-9一二三四五六七八九十百零]+\s*[季集话話]", stem):
        return False
    return True


def clear_parsed_title_for_parent_merge(meta: MetaBase) -> None:
    meta.cn_name = None
    meta.en_name = None
