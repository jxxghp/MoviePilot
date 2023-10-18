from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.transfer import TransferChain
from app.core.security import verify_token
from app.db import get_db
from app.db.models.transferhistory import TransferHistory
from app.schemas import MediaType

router = APIRouter()


@router.post("/manual", summary="手动转移", response_model=schemas.Response)
def manual_transfer(path: str = None,
                    logid: int = None,
                    target: str = None,
                    tmdbid: int = None,
                    type_name: str = None,
                    season: int = None,
                    transfer_type: str = None,
                    episode_format: str = None,
                    episode_detail: str = None,
                    episode_part: str = None,
                    episode_offset: int = 0,
                    min_filesize: int = 0,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    手动转移，文件或历史记录，支持自定义剧集识别格式
    :param path: 转移路径或文件
    :param logid: 转移历史记录ID
    :param target: 目标路径
    :param type_name: 媒体类型、电影/电视剧
    :param tmdbid: tmdbid
    :param season: 剧集季号
    :param transfer_type: 转移类型，move/copy
    :param episode_format: 剧集识别格式
    :param episode_detail: 剧集识别详细信息
    :param episode_part: 剧集识别分集信息
    :param episode_offset: 剧集识别偏移量
    :param min_filesize: 最小文件大小(MB)
    :param db: 数据库
    :param _: Token校验
    """
    force = False
    if logid:
        # 查询历史记录
        history = TransferHistory.get(db, logid)
        if not history:
            return schemas.Response(success=False, message=f"历史记录不存在，ID：{logid}")
        # 强制转移
        force = True
        # 源路径
        in_path = Path(history.src)
        # 目的路径
        if history.dest and str(history.dest) != "None":
            # 删除旧的已整理文件
            TransferChain().delete_files(Path(history.dest))
            if not target:
                target = history.dest
    elif path:
        in_path = Path(path)
    else:
        return schemas.Response(success=False, message=f"缺少参数：path/logid")

    if target and target != "None":
        target = Path(target)
    else:
        target = None

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
    state, errormsg = TransferChain().manual_transfer(
        in_path=in_path,
        target=target,
        tmdbid=tmdbid,
        mtype=mtype,
        season=season,
        transfer_type=transfer_type,
        epformat=epformat,
        min_filesize=min_filesize,
        force=force
    )
    # 失败
    if not state:
        if isinstance(errormsg, list):
            errormsg = f"整理完成，{len(errormsg)} 个文件转移失败！"
        return schemas.Response(success=False, message=errormsg)
    # 成功
    return schemas.Response(success=True)
