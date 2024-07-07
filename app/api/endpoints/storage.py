from pathlib import Path
from typing import Any, List

from fastapi import APIRouter, Depends
from starlette.responses import FileResponse

from app import schemas
from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.metainfo import MetaInfoPath
from app.core.security import verify_token, verify_uri_token
from app.db.user_oper import get_current_active_superuser
from app.helper.progress import ProgressHelper
from app.schemas.types import ProgressKey

router = APIRouter()


@router.get("/qrcode", summary="生成二维码内容", response_model=schemas.Response)
def qrcode(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    生成二维码
    """
    qrcode_data, errmsg = StorageChain().generate_qrcode()
    if qrcode_data:
        return schemas.Response(success=True, data=qrcode_data, message=errmsg)
    return schemas.Response(success=False)


@router.get("/check", summary="二维码登录确认", response_model=schemas.Response)
def check(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    二维码登录确认
    """
    data, errmsg = StorageChain().check_login()
    if data:
        return schemas.Response(success=True, data=data)
    return schemas.Response(success=False, message=errmsg)


@router.post("/list", summary="所有目录和文件", response_model=List[schemas.FileItem])
def list(fileitem: schemas.FileItem,
         sort: str = 'updated_at',
         _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    查询当前目录下所有目录和文件
    :param fileitem: 文件项
    :param sort: 排序方式，name:按名称排序，time:按修改时间排序
    :param _: token
    :return: 所有目录和文件
    """
    file_list = StorageChain().list_files(fileitem)
    if sort == "name":
        file_list.sort(key=lambda x: x.name)
    else:
        file_list.sort(key=lambda x: x.modify_time, reverse=True)
    return file_list


@router.post("/mkdir", summary="创建目录", response_model=schemas.Response)
def mkdir(fileitem: schemas.FileItem,
          name: str,
          _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    创建目录
    :param fileitem: 文件项
    :param name: 目录名称
    :param _: token
    """
    if not name:
        return schemas.Response(success=False)
    result = StorageChain().create_folder(fileitem, name)
    if result:
        return schemas.Response(success=True)
    return schemas.Response(success=False)


@router.post("/delete", summary="删除文件或目录", response_model=schemas.Response)
def delete(fileitem: schemas.FileItem,
           _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    删除文件或目录
    :param fileitem: 文件项
    :param _: token
    """
    result = StorageChain().delete_file(fileitem)
    if result:
        return schemas.Response(success=True)
    return schemas.Response(success=False)


@router.get("/download", summary="下载文件")
def download(fileitem: schemas.FileItem,
             _: schemas.TokenPayload = Depends(verify_uri_token)) -> Any:
    """
    下载文件或目录
    :param fileitem: 文件项
    :param _: token
    """
    # 临时目录
    tmp_file = settings.TEMP_PATH / fileitem.name
    status = StorageChain().download_file(fileitem, tmp_file)
    if status:
        return FileResponse(path=tmp_file)
    return schemas.Response(success=False)


@router.post("/rename", summary="重命名文件或目录", response_model=schemas.Response)
def rename(fileitem: schemas.FileItem,
           new_name: str,
           recursive: bool = False,
           _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    重命名文件或目录
    :param fileitem: 文件项
    :param new_name: 新名称
    :param recursive: 是否递归修改
    :param _: token
    """
    if not fileitem.fileid or not new_name:
        return schemas.Response(success=False)
    result = StorageChain().rename_file(fileitem, new_name)
    if result:
        if recursive:
            transferchain = TransferChain()
            media_exts = settings.RMT_MEDIAEXT + settings.RMT_SUBEXT + settings.RMT_AUDIO_TRACK_EXT
            # 递归修改目录内文件（智能识别命名）
            sub_files: List[schemas.FileItem] = StorageChain().list_files(fileitem)
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
                    sub_path = Path(f"{fileitem.path}{sub_file.name}")
                    meta = MetaInfoPath(sub_path)
                    mediainfo = transferchain.recognize_media(meta)
                    if not mediainfo:
                        progress.end(ProgressKey.BatchRename)
                        return schemas.Response(success=False, message=f"{sub_path.name} 未识别到媒体信息")
                    new_path = transferchain.recommend_name(meta=meta, mediainfo=mediainfo)
                    if not new_path:
                        progress.end(ProgressKey.BatchRename)
                        return schemas.Response(success=False, message=f"{sub_path.name} 未识别到新名称")
                    ret: schemas.Response = rename(fileitem=sub_file,
                                                   new_name=Path(new_path).name,
                                                   recursive=False)
                    if not ret.success:
                        progress.end(ProgressKey.BatchRename)
                        return schemas.Response(success=False, message=f"{sub_path.name} 重命名失败！")
                progress.end(ProgressKey.BatchRename)
        return schemas.Response(success=True)
    return schemas.Response(success=False)
