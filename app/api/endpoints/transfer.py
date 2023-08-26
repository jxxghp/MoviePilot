from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.media import MediaChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.core.security import verify_token
from app.db import get_db
from app.schemas import MediaType

router = APIRouter()


@router.post("/manual", summary="手动转移", response_model=schemas.Response)
def manual_transfer(path: str,
                    tmdbid: int,
                    type_name: str,
                    target: str = None,
                    season: int = None,
                    transfer_type: str = settings.TRANSFER_TYPE,
                    episode_format: str = None,
                    episode_detail: str = None,
                    episode_part: str = None,
                    episode_offset: int = 0,
                    min_filesize: int = 0,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    手动转移，支持自定义剧集识别格式
    :param path: 转移路径或文件
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
    in_path = Path(path)
    if target:
        target = Path(target)
        if not target.exists():
            return schemas.Response(success=False, message=f"目标路径不存在")
    # 识别元数据
    meta = MetaInfo(in_path.stem)
    mtype = MediaType(type_name)
    # 整合数据
    meta.type = mtype
    if season:
        meta.begin_season = season
    # 识别媒体信息
    mediainfo: MediaInfo = MediaChain(db).recognize_media(tmdbid=tmdbid, mtype=mtype)
    if not mediainfo:
        return schemas.Response(success=False, message=f"媒体信息识别失败，tmdbid: {tmdbid}")
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
    state, errormsg = TransferChain(db).manual_transfer(
        in_path=in_path,
        mediainfo=mediainfo,
        transfer_type=transfer_type,
        target=target,
        meta=meta,
        epformat=epformat,
        min_filesize=min_filesize
    )
    # 失败
    if not state:
        return schemas.Response(success=False, message=errormsg)
    # 成功
    return schemas.Response(success=True)
