from typing import Any, List

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.download import DownloadChain
from app.core.security import verify_token

router = APIRouter()


@router.get("/", summary="正在下载", response_model=List[schemas.DownloadingTorrent])
async def read_downloading(
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询正在下载的任务
    """
    return DownloadChain().downloading()


@router.put("/{hashString}/start", summary="开始任务", response_model=schemas.Response)
async def start_downloading(
        hashString: str,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    开如下载任务
    """
    ret = DownloadChain().set_downloading(hashString, "start")
    return schemas.Response(success=True if ret else False)


@router.put("/{hashString}/stop", summary="暂停任务", response_model=schemas.Response)
async def stop_downloading(
        hashString: str,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    控制下载任务
    """
    ret = DownloadChain().set_downloading(hashString, "stop")
    return schemas.Response(success=True if ret else False)


@router.delete("/{hashString}", summary="删除下载任务", response_model=schemas.Response)
async def remove_downloading(
        hashString: str,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    控制下载任务
    """
    ret = DownloadChain().remove_downloading(hashString)
    return schemas.Response(success=True if ret else False)
