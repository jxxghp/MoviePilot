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


@router.put("/{hashString}", summary="开始/暂停", response_model=schemas.Response)
async def set_downloading(
        hashString: str,
        oper: str,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    控制下载任务
    """
    return DownloadChain().set_downloading(hashString, oper)


@router.delete("/{hashString}", summary="删除下载任务", response_model=schemas.Response)
async def remove_downloading(
        hashString: str,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    控制下载任务
    """
    return DownloadChain().remove_downloading(hashString)
