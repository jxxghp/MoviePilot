"""123 云盘目录项与宿主文件项之间的转换。

文件项的 ``storage`` 装的是**存储令牌**而不是存储标识：``p123`` 指本类型的默认实例，
``p123@主号`` 指名为「主号」的那一个。整理编排会拿着文件项回头找存储操作对象，令牌
少了实例段就会落到另一个账号上，因此令牌由调用方按自身归属交进来，不在本模块推定。

下载要用的 ``Etag``/``S3KeyFlag``/``Size`` 随文件项一起带走，存在 ``pickcode`` 里：
浏览一次目录就已经拿到了这些值，下载时再查一次接口既慢又多一次风控。
"""

import json
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Mapping, Optional

from app.schemas import FileItem

# 123 云盘用 Type 区分目录与文件
TYPE_DIRECTORY = 1

# 下载接口需要、又只能从目录项里取到的字段
_DOWNLOAD_HINT_KEYS = ("Etag", "S3KeyFlag", "Size")


def is_directory(entry: Mapping[str, Any]) -> bool:
    """
    判断一条目录项是不是目录

    :param entry: 接口返回的目录项
    :return: 条目为目录时为 True
    """
    return entry.get("Type") == TYPE_DIRECTORY


def join_path(parent: str, name: str, *, is_directory: bool) -> str:
    """
    拼接子项在存储内的绝对路径

    :param parent: 父目录路径
    :param name: 子项名称
    :param is_directory: 子项是否为目录
    :return: 绝对路径，目录带尾部斜杠
    """
    path = PurePosixPath(parent or "/") / name
    return f"{path.as_posix()}/" if is_directory else path.as_posix()


def item_path(path: str, *, is_directory: bool) -> str:
    """
    按条目类型整形其绝对路径

    :param path: 条目路径
    :param is_directory: 条目是否为目录
    :return: 绝对路径，目录带尾部斜杠
    """
    normalized = PurePosixPath(path or "/").as_posix()
    if not is_directory:
        return normalized
    return normalized if normalized.endswith("/") else f"{normalized}/"


def download_hints(entry: Mapping[str, Any]) -> str:
    """
    提取下载接口所需的字段并序列化

    :param entry: 接口返回的目录项
    :return: JSON 文本，供 `parse_download_hints` 还原
    """
    return json.dumps(
        {key: entry.get(key) for key in _DOWNLOAD_HINT_KEYS}, ensure_ascii=False
    )


def parse_download_hints(pickcode: Optional[str]) -> dict:
    """
    还原文件项携带的下载字段

    :param pickcode: 文件项的 ``pickcode``
    :return: 下载字段字典；内容缺失或不是 JSON 对象时为空字典
    """
    if not pickcode:
        return {}
    try:
        parsed = json.loads(pickcode)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_modify_time(value: Any) -> Optional[int]:
    """
    把接口返回的更新时间转成时间戳

    :param value: 接口返回的 ISO 格式时间文本
    :return: 秒级时间戳；取值缺失或无法解析时为 None
    """
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except ValueError:
        return None


def build_file_item(
    entry: Mapping[str, Any], *, storage_token: str, path: str
) -> FileItem:
    """
    把 123 云盘的一条目录项转成宿主文件项

    :param entry: 接口返回的目录项，键名沿用 123 云盘的大驼峰写法
    :param storage_token: 该条目所属的存储令牌，形如 p123 或 p123@主号
    :param path: 该条目在存储内的绝对路径
    :return: 文件项，目录的 ``path`` 带尾部斜杠
    """
    directory = is_directory(entry)
    name = str(entry.get("FileName") or "")
    suffix = PurePosixPath(name).suffix
    return FileItem(
        storage=storage_token,
        fileid=_as_text(entry.get("FileId")),
        parent_fileid=_as_text(entry.get("ParentFileId")),
        name=name,
        basename=PurePosixPath(name).stem,
        extension=None if directory or not suffix else suffix[1:],
        type="dir" if directory else "file",
        path=item_path(path, is_directory=directory),
        size=None if directory else entry.get("Size"),
        modify_time=parse_modify_time(entry.get("UpdateAt")),
        pickcode=download_hints(entry),
    )


def _as_text(value: Any) -> Optional[str]:
    """
    把接口返回的标识转成文本

    :param value: 接口返回的标识
    :return: 文本形式的标识；取值缺失时为 None
    """
    return None if value is None else str(value)
