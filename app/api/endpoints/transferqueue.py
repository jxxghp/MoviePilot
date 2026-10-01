from fastapi import Depends, Query

from app.adapters.web.security.access import verify_token
from app.api.response import COLLECTION_MAX_PAGE_SIZE, ResponseAPIRouter
from app.chain.transfer.facade import TransferChain
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.transfer import TransferQueuePageData as _SchemaTransferQueuePageData

router = ResponseAPIRouter()


@router.get(  # type: ignore[misc]
    "/queue/page",
    summary="查询受限整理队列快照",
    response_model=_SchemaTransferQueuePageData,
)
async def query_queue_page(
    _: _SchemaTokenPayload = Depends(verify_token),
    page: int = Query(1, ge=1),
    count: int = Query(100, ge=1, le=COLLECTION_MAX_PAGE_SIZE),
) -> _SchemaTransferQueuePageData:
    """返回前端所需的有限整理队列窗口，避免全量队列阻塞接口序列化。"""
    items, total = TransferChain().get_queue_tasks_page(page, count)
    return _SchemaTransferQueuePageData(
        items=items,
        total=total,
        page=page,
        count=count,
    )
