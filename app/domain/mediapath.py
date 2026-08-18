"""媒体库根目录的路径推导。

推导只依据重命名格式与重命名后的路径，不读取任何配置、不产生运行日志；
模板与路径的异常以返回值描述，由调用方决定如何呈现。
"""

import re
from pathlib import Path
from typing import NamedTuple, Optional

from app.schemas.types import MediaType

JINJA2_VAR_PATTERN = re.compile(r"\{\{.*?}}", re.DOTALL)
DISC_FOLDER_PATTERN = re.compile(r"(?:cd|disc|disk)\s*0*\d+", re.IGNORECASE)


class MediaRootPathResult(NamedTuple):
    """
    媒体根路径推导结果。

    :param path: 媒体文件根路径；无法推导时为 None
    :param warning: 重命名格式可用但不完整的描述；无异常时为 None
    :param error: 导致推导失败的描述；推导成功时为 None
    """

    path: Optional[Path]
    warning: Optional[str] = None
    error: Optional[str] = None


def resolve_media_root_path(
        rename_format: str,
        rename_path: Path,
        media_type: Optional[MediaType] = None,
) -> MediaRootPathResult:
    """
    推导重命名后的媒体文件根路径。

    :param rename_format: 重命名格式
    :param rename_path: 重命名后的路径
    :param media_type: 媒体类型；音乐需要避开可选碟片目录并返回专辑目录
    :return: 媒体根路径推导结果
    """
    if not rename_format:
        return MediaRootPathResult(path=None, error="重命名格式不能为空")
    if media_type == MediaType.MUSIC:
        # 音乐模板允许按多碟动态增加 Disc 子目录，不能按静态模板层数反推。
        # 文件的直接父目录通常就是专辑目录；命中碟片目录时再上移一级。
        media_root = rename_path.parent
        if DISC_FOLDER_PATTERN.fullmatch(media_root.name):
            media_root = media_root.parent
        return MediaRootPathResult(path=media_root)
    # 计算重命名中的文件夹层数
    rename_list = rename_format.split("/")
    rename_format_level = len(rename_list) - 1
    warning: Optional[str] = None
    # 反向查找标题参数所在层
    for level, name in enumerate(reversed(rename_list)):
        if level == 0:
            # 跳过文件名的标题参数
            continue
        matchs = JINJA2_VAR_PATTERN.findall(name)
        if not matchs:
            continue
        # 处理特例，有的人重命名的第一层是年份、分辨率
        if (any("title" in m for m in matchs)
                and not any("season" in m for m in matchs)):
            # 找出最后一层含有标题且不含季参数的目录作为媒体根目录
            rename_format_level = level
            break
    else:
        # 假定第一层目录是媒体根目录
        warning = f"重命名格式 {rename_format} 缺少标题目录"
    if rename_format_level > len(rename_path.parents):
        # 通常因为路径以/结尾，被Path规范化删除了
        return MediaRootPathResult(
            path=None,
            warning=warning,
            error=f"路径 {rename_path} 不匹配重命名格式 {rename_format}",
        )
    if rename_format_level <= 0:
        # 所有媒体文件都存在一个目录内的特殊需求
        rename_format_level = 1
    # 媒体根路径
    return MediaRootPathResult(
        path=rename_path.parents[rename_format_level - 1],
        warning=warning,
    )
