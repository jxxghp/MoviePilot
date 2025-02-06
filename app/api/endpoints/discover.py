from typing import Any, List

from fastapi import APIRouter, Depends

from app import schemas
from app.core.event import eventmanager
from app.core.security import verify_token
from app.schemas import DiscoverSourceEventData
from app.schemas.types import ChainEventType

router = APIRouter()


@router.get("/source", summary="获取发现数据源", response_model=List[schemas.DiscoverMediaSource])
def source(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    获取发现数据源
    """
    # 广播事件，请示额外的发现数据源支持
    event_data = DiscoverSourceEventData()
    event = eventmanager.send_event(ChainEventType.DiscoverSource, event_data)
    # 使用事件返回的上下文数据
    if event and event.event_data:
        event_data: DiscoverSourceEventData = event.event_data
        if event_data.extra_sources:
            return event_data.extra_sources
    return []
