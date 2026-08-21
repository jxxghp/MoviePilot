"""123 云盘的文件上传与下载。

上传优先走秒传：本地算出 MD5 交给接口，云端已有同样内容时直接建立引用，一个字节
都不必上传。秒传不成立才按分片大小决定分片上传还是整包上传。

上传与下载都向宿主进度表汇报，并在每一片之间检查一次用户是否已取消整理——大文件
的一次传输动辄十几分钟，只在开头检查一次等于取消不了。
"""

import time
from hashlib import md5
from pathlib import Path
from typing import Optional

import requests

from app.schemas import FileItem
from app.sdk.config import global_vars
from app.sdk.logging import logger
from app.sdk.storage import transfer_process

from .api import P123Api
from .client import check_response
from .fileitem import build_file_item, join_path, parse_download_hints

# 下载时每次读取的字节数
_DOWNLOAD_CHUNK_SIZE = 10 * 1024 * 1024

# 计算本地文件 MD5 时每次读取的字节数
_HASH_CHUNK_SIZE = 4096

# 单个分片上传失败后的重试次数与每次重试前的等待秒数
_UPLOAD_RETRY_LIMIT = 5
_UPLOAD_RETRY_INTERVAL_SECONDS = 10

# 上传预签名地址的请求参数，鉴权头必须留空，否则对象存储会拒绝
_UPLOAD_REQUEST_KWARGS = {
    "method": "PUT",
    "headers": {"authorization": ""},
    "parse": ...,
    "timeout": 300,
}

# 上传时的重名处理方式：保留两者并自动加后缀
_UPLOAD_DUPLICATE_POLICY = 2

# 进度百分比的满值
_PROGRESS_COMPLETE = 100


def download(api: P123Api, fileitem: FileItem, local_path: Path) -> Optional[Path]:
    """
    下载文件到本地

    :param api: 本存储实例的接口封装
    :param fileitem: 待下载的文件项
    :param local_path: 本地落盘的完整路径，调用方须先校验其未越出目标目录
    :return: 本地文件路径；获取下载地址失败、传输失败或用户取消时为 None
    """
    hints = parse_download_hints(fileitem.pickcode)
    try:
        response = api.client.download_info(
            {
                "Etag": hints.get("Etag"),
                "FileID": int(fileitem.fileid),
                "FileName": fileitem.name,
                "S3KeyFlag": hints.get("S3KeyFlag"),
                "Size": int(hints.get("Size") or fileitem.size or 0),
            }
        )
        check_response(response)
        download_url = response["data"]["DownloadUrl"]
    except Exception as error:
        logger.error(f"【123云盘】获取 {fileitem.name} 的下载地址失败：{error}")
        return None

    logger.info(f"【123云盘】开始下载：{fileitem.name} -> {local_path}")
    report = transfer_process(Path(fileitem.path).as_posix())
    try:
        with requests.get(download_url, stream=True) as stream:
            stream.raise_for_status()
            written = 0
            with open(local_path, "wb") as target:
                for chunk in stream.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                    if global_vars.is_transfer_stopped(fileitem.path):
                        logger.info(f"【123云盘】{fileitem.path} 下载已取消")
                        return _discard(local_path)
                    if not chunk:
                        continue
                    target.write(chunk)
                    written += len(chunk)
                    if fileitem.size:
                        report(written * _PROGRESS_COMPLETE / fileitem.size)
        report(_PROGRESS_COMPLETE)
    except Exception as error:
        logger.error(f"【123云盘】下载 {fileitem.name} 失败：{error}")
        return _discard(local_path)
    logger.info(f"【123云盘】下载完成：{fileitem.name}")
    return local_path


def upload(
    api: P123Api,
    target_dir: FileItem,
    local_path: Path,
    new_name: Optional[str] = None,
) -> Optional[FileItem]:
    """
    上传本地文件到指定目录

    :param api: 本存储实例的接口封装
    :param target_dir: 上传目标目录项
    :param local_path: 本地文件路径
    :param new_name: 上传后的文件名，为空时沿用本地文件名
    :return: 上传后的文件项；上传失败或用户取消时为 None
    """
    target_name = new_name or local_path.name
    target_path = join_path(target_dir.path, target_name, is_directory=False)
    file_size = local_path.stat().st_size

    try:
        parent_id = target_dir.fileid or api.path_to_id(target_dir.path)
        response = api.client.upload_request(
            {
                "etag": _file_md5(local_path),
                "fileName": target_name,
                "size": file_size,
                "parentFileId": int(parent_id),
                "type": 0,
                "duplicate": _UPLOAD_DUPLICATE_POLICY,
            }
        )
        check_response(response)
    except Exception as error:
        logger.error(f"【123云盘】{target_name} 申请上传失败：{error}")
        return None

    upload_data = response["data"]
    if upload_data.get("Reuse"):
        logger.info(f"【123云盘】{target_name} 秒传成功")
        return _finished_item(api, upload_data.get("Info") or {}, target_path)

    try:
        slice_size = int(upload_data["SliceSize"])
        if file_size > slice_size:
            uploaded = _upload_in_slices(api, upload_data, local_path, file_size, slice_size)
        else:
            uploaded = _upload_whole(api, upload_data, local_path, target_name)
        if not uploaded:
            return None
        upload_data["isMultipart"] = file_size > slice_size
        completed = api.client.upload_complete(upload_data)
        check_response(completed)
    except Exception as error:
        logger.error(f"【123云盘】{target_name} 上传失败：{error}")
        return None

    info = (completed.get("data") or {}).get("file_info") or {}
    return _finished_item(api, info, target_path)


def _upload_in_slices(
    api: P123Api, upload_data: dict, local_path: Path, file_size: int, slice_size: int
) -> bool:
    """
    分片上传本地文件

    :param api: 本存储实例的接口封装
    :param upload_data: 申请上传时接口返回的上传上下文，会被就地补上分片序号
    :param local_path: 本地文件路径
    :param file_size: 本地文件字节数
    :param slice_size: 单个分片的字节数
    :return: 全部分片上传成功时为 True；用户取消时为 False
    :raises Exception: 某一分片重试耗尽后仍失败
    """
    logger.info(
        f"【123云盘】开始分片上传：{local_path}，分片大小 {slice_size} 字节"
    )
    report = transfer_process(local_path.as_posix())
    offset = 0
    with open(local_path, "rb") as source:
        for index, chunk in enumerate(iter(lambda: source.read(slice_size), b""), start=1):
            if global_vars.is_transfer_stopped(local_path.as_posix()):
                logger.info(f"【123云盘】{local_path} 上传已取消")
                return False
            upload_data["partNumberStart"] = index
            upload_data["partNumberEnd"] = index + 1
            _put_with_retry(api, upload_data, str(index), chunk, "upload_prepare")
            offset += len(chunk)
            report(offset * _PROGRESS_COMPLETE / file_size)
    report(_PROGRESS_COMPLETE)
    return True


def _upload_whole(
    api: P123Api, upload_data: dict, local_path: Path, target_name: str
) -> bool:
    """
    整包上传本地文件

    :param api: 本存储实例的接口封装
    :param upload_data: 申请上传时接口返回的上传上下文
    :param local_path: 本地文件路径
    :param target_name: 上传后的文件名，仅用于日志
    :return: 上传成功时为 True
    :raises Exception: 重试耗尽后仍失败
    """
    logger.info(f"【123云盘】开始上传：{local_path} -> {target_name}")
    _put_with_retry(api, upload_data, "1", local_path.read_bytes(), "upload_auth")
    return True


def _put_with_retry(
    api: P123Api, upload_data: dict, part: str, payload: bytes, prepare: str
) -> None:
    """
    向预签名地址投递一片内容，失败时重新取地址后重试

    预签名地址有有效期，重试必须连同地址一起重取，拿着过期地址重试永远不会成功。

    :param api: 本存储实例的接口封装
    :param upload_data: 上传上下文
    :param part: 分片序号，整包上传时固定为 "1"
    :param payload: 待投递的内容
    :param prepare: 取预签名地址的客户端方法名
    :raises Exception: 重试耗尽后仍失败
    """
    last_error: Optional[Exception] = None
    for attempt in range(_UPLOAD_RETRY_LIMIT + 1):
        if attempt:
            time.sleep(_UPLOAD_RETRY_INTERVAL_SECONDS)
        try:
            prepared = getattr(api.client, prepare)(upload_data)
            check_response(prepared)
            api.client.request(
                prepared["data"]["presignedUrls"][part],
                data=payload,
                **_UPLOAD_REQUEST_KWARGS,
            )
            return
        except Exception as error:
            last_error = error
            logger.warning(
                f"【123云盘】分片 {part} 上传失败"
                f"（{attempt + 1}/{_UPLOAD_RETRY_LIMIT + 1}）：{error}"
            )
    raise last_error


def _finished_item(api: P123Api, info: dict, target_path: str) -> Optional[FileItem]:
    """
    把上传完成的接口回执整形为文件项

    :param api: 本存储实例的接口封装
    :param info: 接口回执里的文件信息
    :param target_path: 上传后的完整路径
    :return: 文件项；回执里没有文件标识时为 None
    """
    if not info.get("FileId"):
        logger.error(f"【123云盘】{target_path} 上传回执缺少文件标识")
        return None
    api.remember_path(target_path, info["FileId"])
    return build_file_item(info, storage_token=api.storage_token, path=target_path)


def _file_md5(local_path: Path) -> str:
    """
    计算本地文件的 MD5

    :param local_path: 本地文件路径
    :return: 十六进制 MD5 文本
    """
    digest = md5()
    with open(local_path, "rb") as source:
        for chunk in iter(lambda: source.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _discard(local_path: Path) -> None:
    """
    删除下载了一半的本地文件

    :param local_path: 本地文件路径
    :return: 恒为 None，供调用方直接返回
    """
    if local_path.exists():
        local_path.unlink()
    return None
