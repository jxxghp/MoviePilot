import base64
from pathlib import Path
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import Response

from app import schemas
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.metainfo import MetaInfoPath
from app.core.security import verify_token, verify_uri_token
from app.helper.progress import ProgressHelper
from app.helper.u115 import U115Helper
from app.schemas.types import ProgressKey
from app.utils.http import RequestUtils

router = APIRouter()


@router.get("/qrcode", summary="生成二维码内容", response_model=schemas.Response)
def qrcode(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    生成二维码
    """
    qrcode_data = U115Helper().generate_qrcode()
    if qrcode_data:
        return schemas.Response(success=True, data={
            'codeContent': qrcode_data
        })
    return schemas.Response(success=False)


@router.get("/check", summary="二维码登录确认", response_model=schemas.Response)
def check(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    二维码登录确认
    """
    data, errmsg = U115Helper().check_login()
    if data:
        return schemas.Response(success=True, data=data)
    return schemas.Response(success=False, message=errmsg)


@router.get("/storage", summary="查询存储空间信息", response_model=schemas.Response)
def storage(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询存储空间信息
    """
    storage_info = U115Helper().get_storage()
    if storage_info:
        return schemas.Response(success=True, data={
            "total": storage_info[0],
            "used": storage_info[1]
        })
    return schemas.Response(success=False)


@router.get("/list", summary="所有目录和文件（115网盘）", response_model=List[schemas.FileItem])
def list_115(path: str,
             fileid: str,
             pickcode: str = None,
             filetype: str = "dir",
             sort: str = 'updated_at',
             _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询当前目录下所有目录和文件
    :param path: 当前路径
    :param fileid: 文件ID
    :param pickcode: 115 pickcode
    :param filetype: 文件类型
    :param sort: 排序方式，name:按名称排序，time:按修改时间排序
    :param _: token
    :return: 所有目录和文件
    """
    if not fileid:
        return []
    if not path:
        path = "/"
    if fileid == "root":
        fileid = "0"
    if filetype == "file":
        name = Path(path).name
        suffix = Path(name).suffix[1:]
        return [schemas.FileItem(
            fileid=fileid,
            type="file",
            path=path.rstrip('/'),
            name=name,
            extension=suffix,
            pickcode=pickcode
        )]
    items = U115Helper().list_files(parent_file_id=fileid)
    if not items:
        return []
    file_list = [schemas.FileItem(
        fileid=item.file_id,
        parent_fileid=item.parent_id,
        type="dir" if item.is_dir else "file",
        path=f"{path}{item.name}" + "/" if item.is_dir else "",
        name=item.name,
        size=item.size,
        extension=Path(item.name).suffix[1:],
        modify_time=item.modified_time.timestamp() if item.modified_time else 0,
        pickcode=item.pickcode
    ) for item in items]
    if sort == "name":
        file_list.sort(key=lambda x: x.name)
    else:
        file_list.sort(key=lambda x: x.modify_time, reverse=True)
    return file_list


@router.get("/mkdir", summary="创建目录（115网盘）", response_model=schemas.Response)
def mkdir_115(fileid: str,
              name: str,
              _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    创建目录
    """
    if not fileid or not name:
        return schemas.Response(success=False)
    result = U115Helper().create_folder(parent_file_id=fileid, name=name)
    if result:
        return schemas.Response(success=True)
    return schemas.Response(success=False)


@router.get("/delete", summary="删除文件或目录（115网盘）", response_model=schemas.Response)
def delete_115(fileid: str,
               _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    删除文件或目录
    """
    if not fileid:
        return schemas.Response(success=False)
    result = U115Helper().delete_file(fileid)
    if result:
        return schemas.Response(success=True)
    return schemas.Response(success=False)


@router.get("/download", summary="下载文件（115网盘）")
def download_115(pickcode: str,
                 _: schemas.TokenPayload = Depends(verify_uri_token)) -> Any:
    """
    下载文件或目录
    """
    if not pickcode:
        return schemas.Response(success=False)
    ticket = U115Helper().download(pickcode)
    if ticket:
        # 请求数据，并以文件流的方式返回
        res = RequestUtils(headers=ticket.headers).get_res(ticket.url)
        if res:
            return Response(content=res.content, media_type="application/octet-stream")
    return schemas.Response(success=False)


@router.get("/rename", summary="重命名文件或目录（115网盘）", response_model=schemas.Response)
def rename_115(fileid: str, new_name: str, path: str,
               recursive: bool = False,
               _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    重命名文件或目录
    """
    if not fileid or not new_name:
        return schemas.Response(success=False)
    result = U115Helper().rename_file(fileid, new_name)
    if result:
        if recursive:
            transferchain = TransferChain()
            media_exts = settings.RMT_MEDIAEXT + settings.RMT_SUBEXT + settings.RMT_AUDIO_TRACK_EXT
            # 递归修改目录内文件（智能识别命名）
            sub_files: List[schemas.FileItem] = list_115(path=path, fileid=fileid)
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
                    sub_path = Path(f"{path}{sub_file.name}")
                    meta = MetaInfoPath(sub_path)
                    mediainfo = transferchain.recognize_media(meta)
                    if not mediainfo:
                        progress.end(ProgressKey.BatchRename)
                        return schemas.Response(success=False, message=f"{sub_path.name} 未识别到媒体信息")
                    new_path = transferchain.recommend_name(meta=meta, mediainfo=mediainfo)
                    if not new_path:
                        progress.end(ProgressKey.BatchRename)
                        return schemas.Response(success=False, message=f"{sub_path.name} 未识别到新名称")
                    ret: schemas.Response = rename_115(fileid=sub_file.fileid,
                                                       path=path,
                                                       new_name=Path(new_path).name,
                                                       recursive=False)
                    if not ret.success:
                        progress.end(ProgressKey.BatchRename)
                        return schemas.Response(success=False, message=f"{sub_path.name} 重命名失败！")
                progress.end(ProgressKey.BatchRename)
        return schemas.Response(success=True)
    return schemas.Response(success=False)


@router.get("/image", summary="读取图片（115网盘）")
def image_115(pickcode: str, _: schemas.TokenPayload = Depends(verify_uri_token)) -> Any:
    """
    读取图片
    """
    if not pickcode:
        return schemas.Response(success=False)
    ticket = U115Helper().download(pickcode)
    if ticket:
        # 请求数据，获取内容编码为图片base64返回
        res = RequestUtils(headers=ticket.headers).get_res(ticket.url)
        if res:
            content_type = res.headers.get("Content-Type")
            return Response(content=res.content, media_type=content_type)
    raise HTTPException(status_code=500, detail="下载图片出错")
