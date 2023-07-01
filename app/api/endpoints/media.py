from typing import List, Any

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.media import MediaChain
from app.core.security import verify_token

router = APIRouter()


@router.get("/recognize", summary="识别媒体信息", response_model=schemas.Context)
async def recognize(title: str,
                    subtitle: str = None,
                    _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据标题、副标题识别媒体信息
    """
    # 识别媒体信息
    context = MediaChain().recognize_by_title(title=title, subtitle=subtitle)
    return context.to_dict()


@router.get("/search", summary="搜索媒体信息", response_model=List[schemas.MediaInfo])
async def search_by_title(title: str,
                          page: int = 1,
                          count: int = 8,
                          _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    模糊搜索媒体信息列表
    """
    _, medias = MediaChain().search(title=title)
    if medias:
        return [media.to_dict() for media in medias[(page-1) * count: page * count]]
    return []
