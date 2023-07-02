from typing import Any, List

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.download import DownloadChain
from app.core.security import verify_token

router = APIRouter()


@router.get("/", summary="正在下载", response_model=List[schemas.DownloadingTorrent])
async def read_subscribes(
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询正在下载的任务
    """
    return DownloadChain().downloading()
