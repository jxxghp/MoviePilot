import shutil
from pathlib import Path
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import FileResponse, Response

from app import schemas
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.metainfo import MetaInfoPath
from app.core.security import verify_token, verify_uri_token
from app.helper.progress import ProgressHelper
from app.log import logger
from app.schemas.types import ProgressKey
from app.utils.system import SystemUtils

router = APIRouter()

IMAGE_TYPES = [".jpg", ".png", ".gif", ".bmp", ".jpeg", ".webp"]


@router.post("/list", summary="所有目录和文件（本地）", response_model=List[schemas.FileItem])
def list_local(fileitem: schemas.FileItem,
               sort: str = 'time',
               _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询当前目录下所有目录和文件
    :param fileitem: 文件项
    :param sort: 排序方式，name:按名称排序，time:按修改时间排序
    :param _: token
    :return: 所有目录和文件
    """
    # 返回结果
    ret_items = []
    path = fileitem.path
    if not fileitem.path or fileitem.path == "/":
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
        if SystemUtils.is_windows():
            path = path.lstrip("/")
        elif not path.startswith("/"):
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


@router.get("/listdir", summary="所有目录（本地，不含文件）", response_model=List[schemas.FileItem])
def list_local_dir(path: str, _: schemas.TokenPayload = Depends(verify_token)) -> Any:
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


@router.post("/mkdir", summary="创建目录（本地）", response_model=schemas.Response)
def mkdir_local(fileitem: schemas.FileItem,
                name: str,
                _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    创建目录
    """
    if not fileitem.path:
        return schemas.Response(success=False)
    path_obj = Path(fileitem.path) / name
    if path_obj.exists():
        return schemas.Response(success=False)
    path_obj.mkdir(parents=True, exist_ok=True)
    return schemas.Response(success=True)


@router.post("/delete", summary="删除文件或目录（本地）", response_model=schemas.Response)
def delete_local(fileitem: schemas.FileItem, _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    删除文件或目录
    """
    if not fileitem.path:
        return schemas.Response(success=False)
    path_obj = Path(fileitem.path)
    if not path_obj.exists():
        return schemas.Response(success=True)
    if path_obj.is_file():
        path_obj.unlink()
    else:
        shutil.rmtree(path_obj, ignore_errors=True)
    return schemas.Response(success=True)


@router.get("/download", summary="下载文件（本地）")
def download_local(path: str, _: schemas.TokenPayload = Depends(verify_uri_token)) -> Any:
    """
    下载文件或目录
    """
    if not path:
        return schemas.Response(success=False)
    path_obj = Path(path)
    if not path_obj.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
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


@router.post("/rename", summary="重命名文件或目录（本地）", response_model=schemas.Response)
def rename_local(fileitem: schemas.FileItem,
                 new_name: str,
                 recursive: bool = False,
                 _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    重命名文件或目录
    """
    if not fileitem.path or not new_name:
        return schemas.Response(success=False)
    path_obj = Path(fileitem.path)
    if not path_obj.exists():
        return schemas.Response(success=False)
    path_obj.rename(path_obj.parent / new_name)
    if recursive:
        transferchain = TransferChain()
        media_exts = settings.RMT_MEDIAEXT + settings.RMT_SUBEXT + settings.RMT_AUDIO_TRACK_EXT
        # 递归修改目录内文件（智能识别命名）
        sub_files: List[schemas.FileItem] = list_local(fileitem=fileitem)
        if sub_files:
            # 开始进度
            progress = ProgressHelper()
            progress.start(ProgressKey.BatchRename)
            total = len(sub_files)
            handled = 0
            for sub_file in sub_files:
                handled += 1
                progress.update(value=handled / total * 100,
                                text=f"正在处理 {sub_file.name} ...",
                                key=ProgressKey.BatchRename)
                if sub_file.type == "dir":
                    continue
                if not sub_file.extension:
                    continue
                if f".{sub_file.extension.lower()}" not in media_exts:
                    continue
                sub_path = Path(sub_file.path)
                meta = MetaInfoPath(sub_path)
                mediainfo = transferchain.recognize_media(meta)
                if not mediainfo:
                    progress.end(ProgressKey.BatchRename)
                    return schemas.Response(success=False, message=f"{sub_path.name} 未识别到媒体信息")
                new_path = transferchain.recommend_name(meta=meta, mediainfo=mediainfo)
                if not new_path:
                    progress.end(ProgressKey.BatchRename)
                    return schemas.Response(success=False, message=f"{sub_path.name} 未识别到新名称")
                ret: schemas.Response = rename_local(fileitem, new_name=Path(new_path).name, recursive=False)
                if not ret.success:
                    progress.end(ProgressKey.BatchRename)
                    return schemas.Response(success=False, message=f"{sub_path.name} 重命名失败！")
            progress.end(ProgressKey.BatchRename)
    return schemas.Response(success=True)


@router.get("/image", summary="读取图片（本地）")
def image_local(path: str, _: schemas.TokenPayload = Depends(verify_uri_token)) -> Any:
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
        raise HTTPException(status_code=500, detail="图片读取出错")
    return Response(content=path_obj.read_bytes(), media_type="image/jpeg")
