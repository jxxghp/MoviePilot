from typing import Any

from fastapi import APIRouter, Depends, BackgroundTasks

from app import schemas
from app.chain.douban import DoubanChain
from app.db.models.user import User
from app.db.userauth import get_current_active_superuser

router = APIRouter()


def start_douban_chain():
    """
    启动链式任务
    """
    DoubanChain().sync()


@router.get("/sync", response_model=schemas.Response)
async def sync_douban(
        background_tasks: BackgroundTasks,
        _: User = Depends(get_current_active_superuser)) -> Any:
    """
    同步豆瓣想看
    """
    background_tasks.add_task(start_douban_chain)
    return schemas.Response(success=True, message="任务已启动")
