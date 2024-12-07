from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.core.metainfo import MetaInfoPath
from app.core.security import verify_token, verify_apitoken
from app.db import get_db
from app.db.models.transferhistory import TransferHistory
from app.db.user_oper import get_current_active_superuser
from app.schemas import FileItem, TransferItem, MediaType

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
def manual_transfer(transer_item: TransferItem,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    手动转移，文件或历史记录，支持自定义剧集识别格式
    :param transer_item: 手工整理项
    :param db: 数据库
    :param _: Token校验
    """
    force = False
    if transer_item.logid:
        # 查询历史记录
        history: TransferHistory = TransferHistory.get(db, transer_item.logid)
        if not history:
            return schemas.Response(success=False, message=f"历史记录不存在，ID：{transer_item.logid}")
        # 强制转移
        force = True
        if history.status and ("move" in history.mode):
            # 重新整理成功的转移，则使用成功的 dest 做 in_path
            transer_item.fileitem = FileItem(**history.dest_fileitem)
        else:
            # 源路径
            transer_item.fileitem = FileItem(**history.src_fileitem)
            # 目的路径
            if history.dest_fileitem:
                # 删除旧的已整理文件
                dest_fileitem = FileItem(**history.dest_fileitem)
                state = StorageChain().delete_media_file(dest_fileitem, mtype=MediaType(history.type))
                if not state:
                    return schemas.Response(success=False, message=f"{dest_fileitem.path} 删除失败")

        # 从历史数据获取信息
        if transer_item.from_history:
            transer_item.type_name = history.type if history.type else transer_item.type_name
            transer_item.tmdbid = int(history.tmdbid) if history.tmdbid else transer_item.tmdbid
            transer_item.doubanid = str(history.doubanid) if history.doubanid else transer_item.doubanid
            transer_item.season = int(str(history.seasons).replace("S", "")) if history.seasons else transer_item.season
            if history.episodes:
                if "-" in str(history.episodes):
                    # E01-E03多集合并
                    episode_start, episode_end = str(history.episodes).split("-")
                    episode_list: list[int] = []
                    for i in range(int(episode_start.replace("E", "")), int(episode_end.replace("E", "")) + 1):
                        episode_list.append(i)
                    transer_item.episode_detail = ",".join(str(e) for e in episode_list)
                else:
                    # E01单集
                    transer_item.episode_detail = str(history.episodes).replace("E", "")

    elif not transer_item.fileitem:
        return schemas.Response(success=False, message=f"缺少参数")
    # 开始转移
    state, errormsg = TransferChain().manual_transfer(
        transfer_item=transer_item,
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
