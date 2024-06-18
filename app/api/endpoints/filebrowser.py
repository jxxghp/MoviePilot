import shutil
from pathlib import Path
from typing import Any, List

from fastapi import APIRouter, Depends
from starlette.responses import FileResponse, Response

from app import schemas
from app.core.config import settings
from app.core.security import verify_token, verify_uri_token
from app.helper.aliyun import AliyunHelper
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.system import SystemUtils

router = APIRouter()

IMAGE_TYPES = [".jpg", ".png", ".gif", ".bmp", ".jpeg", ".webp"]


@router.get("/local/list", summary="所有目录和文件（本地）", response_model=List[schemas.FileItem])
def list_path(path: str,
              sort: str = 'time',
              _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询当前目录下所有目录和文件
    :param path: 目录路径
    :param sort: 排序方式，name:按名称排序，time:按修改时间排序
    :param _: token
    :return: 所有目录和文件
    """
    # 返回结果
    ret_items = []
    if not path or path == "/":
        if SystemUtils.is_windows():
            partitions = SystemUtils.get_windows_drives() or ["C:/"]
            for partition in partitions:
                ret_items.append(schemas.FileItem(
                    type="dir",
                    path=partition + "/",
                    name=partition,
                    basename=partition
                ))
            return ret_items
        else:
            path = "/"
    else:
        if not SystemUtils.is_windows() and not path.startswith("/"):
            path = "/" + path

    # 遍历目录
    path_obj = Path(path)
    if not path_obj.exists():
        logger.warn(f"目录不存在：{path}")
        return []

    # 如果是文件
    if path_obj.is_file():
        ret_items.append(schemas.FileItem(
            type="file",
            path=str(path_obj).replace("\\", "/"),
            name=path_obj.name,
            basename=path_obj.stem,
            extension=path_obj.suffix[1:],
            size=path_obj.stat().st_size,
            modify_time=path_obj.stat().st_mtime,
        ))
        return ret_items

    # 扁历所有目录
    for item in SystemUtils.list_sub_directory(path_obj):
        ret_items.append(schemas.FileItem(
            type="dir",
            path=str(item).replace("\\", "/") + "/",
            name=item.name,
            basename=item.stem,
            modify_time=item.stat().st_mtime,
        ))

    # 遍历所有文件，不含子目录
    for item in SystemUtils.list_sub_files(path_obj,
                                           settings.RMT_MEDIAEXT
                                           + settings.RMT_SUBEXT
                                           + IMAGE_TYPES
                                           + [".nfo"]):
        ret_items.append(schemas.FileItem(
            type="file",
            path=str(item).replace("\\", "/"),
            name=item.name,
            basename=item.stem,
            extension=item.suffix[1:],
            size=item.stat().st_size,
            modify_time=item.stat().st_mtime,
        ))
    # 排序
    if sort == 'time':
        ret_items.sort(key=lambda x: x.modify_time, reverse=True)
    else:
        ret_items.sort(key=lambda x: x.name, reverse=False)
    return ret_items


@router.get("/local/listdir", summary="所有目录（本地，不含文件）", response_model=List[schemas.FileItem])
def list_dir(path: str, _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询当前目录下所有目录
    """
    # 返回结果
    ret_items = []
    if not path or path == "/":
        if SystemUtils.is_windows():
            partitions = SystemUtils.get_windows_drives() or ["C:/"]
            for partition in partitions:
                ret_items.append(schemas.FileItem(
                    type="dir",
                    path=partition + "/",
                    name=partition,
                    children=[]
                ))
            return ret_items
        else:
            path = "/"
    else:
        if not SystemUtils.is_windows() and not path.startswith("/"):
            path = "/" + path

    # 遍历目录
    path_obj = Path(path)
    if not path_obj.exists():
        logger.warn(f"目录不存在：{path}")
        return []

    # 扁历所有目录
    for item in SystemUtils.list_sub_directory(path_obj):
        ret_items.append(schemas.FileItem(
            type="dir",
            path=str(item).replace("\\", "/") + "/",
            name=item.name,
            children=[]
        ))
    return ret_items


@router.get("/local/mkdir", summary="创建目录（本地）", response_model=schemas.Response)
def mkdir(path: str, _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    创建目录
    """
    if not path:
        return schemas.Response(success=False)
    path_obj = Path(path)
    if path_obj.exists():
        return schemas.Response(success=False)
    path_obj.mkdir(parents=True, exist_ok=True)
    return schemas.Response(success=True)


@router.get("/local/delete", summary="删除文件或目录（本地）", response_model=schemas.Response)
def delete(path: str, _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    删除文件或目录
    """
    if not path:
        return schemas.Response(success=False)
    path_obj = Path(path)
    if not path_obj.exists():
        return schemas.Response(success=True)
    if path_obj.is_file():
        path_obj.unlink()
    else:
        shutil.rmtree(path_obj, ignore_errors=True)
    return schemas.Response(success=True)


@router.get("/local/download", summary="下载文件（本地）")
def download(path: str, _: schemas.TokenPayload = Depends(verify_uri_token)) -> Any:
    """
    下载文件或目录
    """
    if not path:
        return schemas.Response(success=False)
    path_obj = Path(path)
    if not path_obj.exists():
        return schemas.Response(success=False)
    if path_obj.is_file():
        # 做为文件流式下载
        return FileResponse(path_obj)
    else:
        # 做为压缩包下载
        shutil.make_archive(base_name=path_obj.stem, format="zip", root_dir=path_obj)
        reponse = Response(content=path_obj.read_bytes(), media_type="application/zip")
        # 删除压缩包
        Path(f"{path_obj.stem}.zip").unlink()
        return reponse


@router.get("/local/rename", summary="重命名文件或目录（本地）", response_model=schemas.Response)
def rename(path: str, new_name: str, _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    重命名文件或目录
    """
    if not path or not new_name:
        return schemas.Response(success=False)
    path_obj = Path(path)
    if not path_obj.exists():
        return schemas.Response(success=False)
    path_obj.rename(path_obj.parent / new_name)
    return schemas.Response(success=True)


@router.get("/local/image", summary="读取图片（本地）")
def image(path: str, _: schemas.TokenPayload = Depends(verify_uri_token)) -> Any:
    """
    读取图片
    """
    if not path:
        return None
    path_obj = Path(path)
    if not path_obj.exists():
        return None
    if not path_obj.is_file():
        return None
    # 判断是否图片文件
    if path_obj.suffix.lower() not in IMAGE_TYPES:
        return None
    return Response(content=path_obj.read_bytes(), media_type="image/jpeg")


@router.get("/aliyun/list", summary="所有目录和文件（阿里云盘）", response_model=List[schemas.FileItem])
def list_path(path: str,
              fileid: str,
              filetype: str,
              sort: str = 'updated_at',
              _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询当前目录下所有目录和文件
    :param path: 当前路径
    :param fileid: 文件ID
    :param filetype: 文件类型
    :param sort: 排序方式，name:按名称排序，time:按修改时间排序
    :param _: token
    :return: 所有目录和文件
    """
    if not fileid:
        return []
    if not path:
        path = "/"
    if sort == "time":
        sort = "updated_at"
    if filetype == "file":
        fileinfo = AliyunHelper().get_file_detail(fileid)
        if fileinfo:
            return [schemas.FileItem(
                fileid=fileinfo.get("file_id"),
                parent_fileid=fileinfo.get("parent_file_id"),
                type="file",
                path=f"{path}{fileinfo.get('name')}",
                name=fileinfo.get("name"),
                size=fileinfo.get("size"),
                extension=fileinfo.get("file_extension"),
                modify_time=StringUtils.str_to_timestamp(fileinfo.get("updated_at")),
                thumbnail=fileinfo.get("thumbnail")
            )]
        return []
    items = AliyunHelper().list_files(parent_file_id=fileid, order_by=sort)
    if not items:
        return []
    return [schemas.FileItem(
        fileid=item.get("file_id"),
        parent_fileid=item.get("parent_file_id"),
        type="dir" if item.get("type") == "folder" else "file",
        path=f"{path}{item.get('name')}" + "/" if item.get("type") == "folder" else "",
        name=item.get("name"),
        size=item.get("size"),
        extension=item.get("file_extension"),
        modify_time=StringUtils.str_to_timestamp(item.get("updated_at")),
        thumbnail=item.get("thumbnail")
    ) for item in items]


@router.get("/aliyun/mkdir", summary="创建目录（阿里云盘）", response_model=schemas.Response)
def mkdir(fileid: str,
          name: str,
          _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    创建目录
    """
    if not fileid or not name:
        return schemas.Response(success=False)
    result = AliyunHelper().create_folder(parent_file_id=fileid, name=name)
    if result:
        return schemas.Response(success=True)
    return schemas.Response(success=False)


@router.get("/aliyun/delete", summary="删除文件或目录（阿里云盘）", response_model=schemas.Response)
def delete(fileid: str,
           _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    删除文件或目录
    """
    if not fileid:
        return schemas.Response(success=False)
    result = AliyunHelper().delete_file(fileid)
    if result:
        return schemas.Response(success=True)
    return schemas.Response(success=False)


@router.get("/aliyun/download", summary="下载文件（阿里云盘）")
def download(fileid: str,
             _: schemas.TokenPayload = Depends(verify_uri_token)) -> Any:
    """
    下载文件或目录
    """
    if not fileid:
        return schemas.Response(success=False)
    url = AliyunHelper().get_download_url(fileid)
    if url:
        # 重定向
        return Response(status_code=302, headers={"Location": url})
    return schemas.Response(success=False)


@router.get("/aliyun/rename", summary="重命名文件或目录（阿里云盘）", response_model=schemas.Response)
def rename(fileid: str, new_name: str, _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    重命名文件或目录
    """
    if not fileid or not new_name:
        return schemas.Response(success=False)
    result = AliyunHelper().rename_file(fileid, new_name)
    if result:
        return schemas.Response(success=True)
    return schemas.Response(success=False)


@router.get("/aliyun/image", summary="读取图片（阿里云盘）", response_model=schemas.Response)
def image(fileid: str, _: schemas.TokenPayload = Depends(verify_uri_token)) -> Any:
    """
    读取图片
    """
    if not fileid:
        return schemas.Response(success=False)
    url = AliyunHelper().get_download_url(fileid)
    if url:
        # 重定向
        return Response(status_code=302, headers={"Location": url})
    return schemas.Response(success=False)
