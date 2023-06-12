from fastapi import APIRouter, Depends, BackgroundTasks

from app import schemas
from app.chain.douban_sync import DoubanSyncChain
from app.db.models.user import User
from app.db.userauth import get_current_active_superuser

router = APIRouter()


def start_douban_chain():
    """
    启动链式任务
    """
    DoubanSyncChain().process()


@router.get("/sync", response_model=schemas.Response)
async def sync_douban(
        background_tasks: BackgroundTasks,
        _: User = Depends(get_current_active_superuser)):
    """
    查询所有订阅
    """
    background_tasks.add_task(start_douban_chain)
    return {"success": True}
