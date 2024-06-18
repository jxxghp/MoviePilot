from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request, Depends

from app import schemas
from app.chain.webhook import WebhookChain
from app.core.security import verify_apitoken

router = APIRouter()


def start_webhook_chain(body: Any, form: Any, args: Any):
    """
    启动链式任务
    """
    WebhookChain().message(body=body, form=form, args=args)


@router.post("/", summary="Webhook消息响应", response_model=schemas.Response)
async def webhook_message(background_tasks: BackgroundTasks,
                          request: Request,
                          _: str = Depends(verify_apitoken)
                          ) -> Any:
    """
    Webhook响应
    """
    body = await request.body()
    form = await request.form()
    args = request.query_params
    background_tasks.add_task(start_webhook_chain, body, form, args)
    return schemas.Response(success=True)


@router.get("/", summary="Webhook消息响应", response_model=schemas.Response)
def webhook_message(background_tasks: BackgroundTasks,
                    request: Request, _: str = Depends(verify_apitoken)) -> Any:
    """
    Webhook响应
    """
    args = request.query_params
    background_tasks.add_task(start_webhook_chain, None, None, args)
    return schemas.Response(success=True)
