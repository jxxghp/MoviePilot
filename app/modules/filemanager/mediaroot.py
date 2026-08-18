"""文件整理使用的媒体库根目录推导。"""

from pathlib import Path
from typing import Optional

from app.domain.mediapath import resolve_media_root_path
from app.runtime.log import logger
from app.schemas.types import MediaType


def get_media_root_path(
        rename_format: str,
        rename_path: Path,
        media_type: Optional[MediaType] = None,
) -> Optional[Path]:
    """
    获取重命名后的媒体文件根路径，并记录重命名格式与路径的异常。

    :param rename_format: 重命名格式
    :param rename_path: 重命名后的路径
    :param media_type: 媒体类型；音乐需要避开可选碟片目录并返回专辑目录
    :return: 媒体文件根路径；重命名格式或路径无效时为 None
    """
    result = resolve_media_root_path(rename_format, rename_path, media_type)
    if result.warning:
        logger.warn(result.warning)
    if result.error:
        logger.error(result.error)
    return result.path
