from typing import List, Any

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.media import MediaChain
from app.db.models.user import User
from app.db.userauth import get_current_active_user

router = APIRouter()


@router.get("/recognize", response_model=schemas.Context)
async def recognize(title: str,
                    subtitle: str = None,
                    _: User = Depends(get_current_active_user)) -> Any:
    """
    识别媒体信息
    """
    # 识别媒体信息
    context = MediaChain().recognize_by_title(title=title, subtitle=subtitle)
    return context.to_dict()


@router.get("/search", response_model=List[schemas.MediaInfo])
async def search_by_title(title: str,
                          _: User = Depends(get_current_active_user)) -> Any:
    """
    模糊搜索媒体信息列表
    """
    _, medias = MediaChain().search(title=title)
    return [media.to_dict() for media in medias]
