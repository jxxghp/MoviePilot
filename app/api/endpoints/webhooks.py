
from fastapi import APIRouter, BackgroundTasks, Request

from app import schemas
from app.chain.webhook_message import WebhookMessageChain
from app.core import settings

router = APIRouter()


def start_webhook_chain(message: dict):
    """
    启动链式任务
    """
    WebhookMessageChain().process(message)


@router.post("/", response_model=schemas.Response)
async def webhook_message(background_tasks: BackgroundTasks, token: str, request: Request):
    """
    Webhook响应
    """
    if token != settings.API_TOKEN:
        return {"success": False, "message": "token认证不通过"}

    background_tasks.add_task(start_webhook_chain, await request.json())
    return {"success": True}
