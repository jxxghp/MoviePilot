from pathlib import Path
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import Response

from app import schemas
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.metainfo import MetaInfoPath
from app.core.security import verify_token, verify_uri_token
from app.helper.aliyun import AliyunHelper
from app.helper.progress import ProgressHelper
from app.schemas.types import ProgressKey

router = APIRouter()


@router.get("/qrcode", summary="生成二维码内容", response_model=schemas.Response)
def qrcode(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    生成二维码
    """
    qrcode_data, errmsg = AliyunHelper().generate_qrcode()
    if qrcode_data:
        return schemas.Response(success=True, data=qrcode_data)
    return schemas.Response(success=False, message=errmsg)


@router.get("/check", summary="二维码登录确认", response_model=schemas.Response)
def check(ck: str, t: str, _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    二维码登录确认
    """
    if not ck or not t:
        return schemas.Response(success=False, message="参数错误")
    data, errmsg = AliyunHelper().check_login(ck, t)
    if data:
        return schemas.Response(success=True, data=data)
    return schemas.Response(success=False, message=errmsg)


@router.get("/userinfo", summary="查询用户信息", response_model=schemas.Response)
def userinfo(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询用户信息
    """
    aliyunhelper = AliyunHelper()
    # 查询用户信息返回
    info = aliyunhelper.user_info()
    if info:
        return schemas.Response(success=True, data=info)
    return schemas.Response(success=False)


@router.post("/list", summary="所有目录和文件（阿里云盘）", response_model=List[schemas.FileItem])
def list_aliyun(fileitem: schemas.FileItem,
                sort: str = 'updated_at',
                _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询当前目录下所有目录和文件
    :param fileitem: 文件夹信息
    :param sort: 排序方式，name:按名称排序，time:按修改时间排序
    :param _: token
    :return: 所有目录和文件
    """
    if not fileitem.fileid:
        return []
    if not fileitem.path:
        path = "/"
    else:
        path = fileitem.path
    if sort == "time":
        sort = "updated_at"
    if fileitem.type == "file":
        fileitem = AliyunHelper().detail(drive_id=fileitem.drive_id, file_id=fileitem.fileid, path=path)
        if fileitem:
            return [fileitem]
        return []
    return AliyunHelper().list(drive_id=fileitem.drive_id,
                               parent_file_id=fileitem.fileid,
                               path=path,
                               order_by=sort)


@router.post("/mkdir", summary="创建目录（阿里云盘）", response_model=schemas.Response)
def mkdir_aliyun(fileitem: schemas.FileItem,
                 name: str,
                 _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    创建目录
    """
    if not fileitem.fileid or not name:
        return schemas.Response(success=False)
    result = AliyunHelper().create_folder(drive_id=fileitem.drive_id, parent_file_id=fileitem.fileid,
                                          name=name, path=fileitem.path)
    if result:
        return schemas.Response(success=True)
    return schemas.Response(success=False)


@router.post("/delete", summary="删除文件或目录（阿里云盘）", response_model=schemas.Response)
def delete_aliyun(fileitem: schemas.FileItem,
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    删除文件或目录
    """
    if not fileitem.fileid:
        return schemas.Response(success=False)
    result = AliyunHelper().delete(drive_id=fileitem.drive_id, file_id=fileitem.fileid)
    if result:
        return schemas.Response(success=True)
    return schemas.Response(success=False)


@router.get("/download", summary="下载文件（阿里云盘）")
def download_aliyun(fileid: str,
                    drive_id: str = None,
                    _: schemas.TokenPayload = Depends(verify_uri_token)) -> Any:
    """
    下载文件或目录
    """
    if not fileid:
        return schemas.Response(success=False)
    url = AliyunHelper().download(drive_id=drive_id, file_id=fileid)
    if url:
        # 重定向
        return Response(status_code=302, headers={"Location": url})
    raise HTTPException(status_code=500, detail="下载文件出错")


@router.post("/rename", summary="重命名文件或目录（阿里云盘）", response_model=schemas.Response)
def rename_aliyun(fileitem: schemas.FileItem,
                  new_name: str,
                  recursive: bool = False,
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    重命名文件或目录
    """
    if not fileitem.fileid or not new_name:
        return schemas.Response(success=False)
    result = AliyunHelper().rename(drive_id=fileitem.drive_id, file_id=fileitem.fileid, name=new_name)
    if result:
        if recursive:
            transferchain = TransferChain()
            media_exts = settings.RMT_MEDIAEXT + settings.RMT_SUBEXT + settings.RMT_AUDIO_TRACK_EXT
            # 递归修改目录内文件（智能识别命名）
            sub_files: List[schemas.FileItem] = list_aliyun(fileitem=fileitem)
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
                    ret: schemas.Response = rename_aliyun(fileitem=sub_file,
                                                          new_name=Path(new_path).name,
                                                          recursive=False)
                    if not ret.success:
                        progress.end(ProgressKey.BatchRename)
                        return schemas.Response(success=False, message=f"{sub_path.name} 重命名失败！")
                progress.end(ProgressKey.BatchRename)
        return schemas.Response(success=True)
    return schemas.Response(success=False)


@router.get("/image", summary="读取图片（阿里云盘）", response_model=schemas.Response)
def image_aliyun(fileid: str, drive_id: str = None, _: schemas.TokenPayload = Depends(verify_uri_token)) -> Any:
    """
    读取图片
    """
    if not fileid:
        return schemas.Response(success=False)
    url = AliyunHelper().download(drive_id=drive_id, file_id=fileid)
    if url:
        # 重定向
        return Response(status_code=302, headers={"Location": url})
    raise HTTPException(status_code=500, detail="下载图片出错")
