import shutil
from pathlib import Path
from typing import Any, List

from fastapi import APIRouter, Depends
from starlette.responses import FileResponse, Response

from app import schemas
from app.core.config import settings
from app.core.security import verify_token
from app.log import logger
from app.utils.system import SystemUtils

router = APIRouter()

IMAGE_TYPES = [".jpg", ".png", ".gif", ".bmp", ".jpeg", ".webp"]


@router.get("/list", summary="所有目录和文件", response_model=List[schemas.FileItem])
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


@router.get("/listdir", summary="所有目录（不含文件）", response_model=List[schemas.FileItem])
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


@router.get("/mkdir", summary="创建目录", response_model=schemas.Response)
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


@router.get("/delete", summary="删除文件或目录", response_model=schemas.Response)
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


@router.get("/download", summary="下载文件或目录")
def download(path: str, token: str) -> Any:
    """
    下载文件或目录
    """
    if not path:
        return schemas.Response(success=False)
    # 认证token
    if not verify_token(token):
        return None
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


@router.get("/rename", summary="重命名文件或目录", response_model=schemas.Response)
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


@router.get("/image", summary="读取图片")
def image(path: str, token: str) -> Any:
    """
    读取图片
    """
    if not path:
        return None
    # 认证token
    if not verify_token(token):
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
