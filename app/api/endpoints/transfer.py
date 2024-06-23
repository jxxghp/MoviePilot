from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.media import MediaChain
from app.chain.transfer import TransferChain
from app.core.metainfo import MetaInfoPath
from app.core.security import verify_token, verify_apitoken
from app.db import get_db
from app.db.models.transferhistory import TransferHistory
from app.schemas import MediaType

router = APIRouter()


@router.get("/name", summary="查询整理后的名称", response_model=schemas.Response)
def query_name(path: str, filetype: str,
               _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询整理后的名称
    :param path: 文件路径
    :param filetype: 文件类型
    :param _: Token校验
    """
    meta = MetaInfoPath(Path(path))
    mediainfo = MediaChain().recognize_media(meta)
    if not mediainfo:
        return schemas.Response(success=False, message="未识别到媒体信息")
    new_path = TransferChain().recommend_name(meta=meta, mediainfo=mediainfo)
    if not new_path:
        return schemas.Response(success=False, message="未识别到新名称")
    if filetype == "dir":
        parents = Path(new_path).parents
        if len(parents) > 2:
            new_name = parents[1].name
        else:
            new_name = parents[0].name
    else:
        new_name = Path(new_path).name
    return schemas.Response(success=True, data={
        "name": new_name
    })


@router.post("/manual", summary="手动转移", response_model=schemas.Response)
def manual_transfer(storage: str = "local",
                    path: str = None,
                    drive_id: str = None,
                    fileid: str = None,
                    filetype: str = None,
                    logid: int = None,
                    target: str = None,
                    tmdbid: int = None,
                    doubanid: str = None,
                    type_name: str = None,
                    season: int = None,
                    transfer_type: str = None,
                    episode_format: str = None,
                    episode_detail: str = None,
                    episode_part: str = None,
                    episode_offset: int = 0,
                    min_filesize: int = 0,
                    scrape: bool = None,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    手动转移，文件或历史记录，支持自定义剧集识别格式
    :param storage: 存储类型：local/aliyun/u115
    :param path: 转移路径或文件
    :param drive_id: 云盘ID（网盘等）
    :param fileid: 文件ID（网盘等）
    :param filetype: 文件类型，dir/file
    :param logid: 转移历史记录ID
    :param target: 目标路径
    :param type_name: 媒体类型、电影/电视剧
    :param tmdbid: tmdbid
    :param doubanid: 豆瓣ID
    :param season: 剧集季号
    :param transfer_type: 转移类型，move/copy 等
    :param episode_format: 剧集识别格式
    :param episode_detail: 剧集识别详细信息
    :param episode_part: 剧集识别分集信息
    :param episode_offset: 剧集识别偏移量
    :param min_filesize: 最小文件大小(MB)
    :param scrape: 是否刮削元数据
    :param db: 数据库
    :param _: Token校验
    """
    force = False
    target = Path(target) if target else None
    transfer = TransferChain()
    if logid:
        # 查询历史记录
        history: TransferHistory = TransferHistory.get(db, logid)
        if not history:
            return schemas.Response(success=False, message=f"历史记录不存在，ID：{logid}")
        # 强制转移
        force = True
        if history.status and ("move" in history.mode):
            # 重新整理成功的转移，则使用成功的 dest 做 in_path
            in_path = Path(history.dest)
        else:
            # 源路径
            in_path = Path(history.src)
            # 目的路径
            if history.dest and str(history.dest) != "None":
                # 删除旧的已整理文件
                transfer.delete_files(Path(history.dest))
    elif path:
        in_path = Path(path)
    else:
        return schemas.Response(success=False, message=f"缺少参数：path/logid")

    # 类型
    mtype = MediaType(type_name) if type_name else None
    # 自定义格式
    epformat = None
    if episode_offset or episode_part or episode_detail or episode_format:
        epformat = schemas.EpisodeFormat(
            format=episode_format,
            detail=episode_detail,
            part=episode_part,
            offset=episode_offset,
        )
    # 开始转移
    state, errormsg = transfer.manual_transfer(
        storage=storage,
        in_path=in_path,
        drive_id=drive_id,
        fileid=fileid,
        filetype=filetype,
        target=target,
        tmdbid=tmdbid,
        doubanid=doubanid,
        mtype=mtype,
        season=season,
        transfer_type=transfer_type,
        epformat=epformat,
        min_filesize=min_filesize,
        scrape=scrape,
        force=force
    )
    # 失败
    if not state:
        if isinstance(errormsg, list):
            errormsg = f"整理完成，{len(errormsg)} 个文件转移失败！"
        return schemas.Response(success=False, message=errormsg)
    # 成功
    return schemas.Response(success=True)


@router.get("/now", summary="立即执行下载器文件整理", response_model=schemas.Response)
def now(_: str = Depends(verify_apitoken)) -> Any:
    """
    立即执行下载器文件整理 API_TOKEN认证（?token=xxx）
    """
    TransferChain().process()
    return schemas.Response(success=True)
