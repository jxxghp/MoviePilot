from fastapi import APIRouter, HTTPException, Depends

from app import schemas
from app.chain.identify import IdentifyChain
from app.db.models.user import User
from app.db.userauth import get_current_active_user

router = APIRouter()


@router.post("/recognize", response_model=schemas.Context)
async def recognize(title: str,
                    subtitle: str = None,
                    current_user: User = Depends(get_current_active_user)):
    """
    识别媒体信息
    """
    if not current_user:
        raise HTTPException(
            status_code=400,
            detail="需要授权",
        )
    # 识别媒体信息
    context = IdentifyChain().process(title=title, subtitle=subtitle)
    return context.to_dict()
