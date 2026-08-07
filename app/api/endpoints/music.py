from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app import schemas
from app.chain.music import MusicChain
from app.core.music import MusicInfo
from app.core.security import verify_token

router = APIRouter()

CountParam = Annotated[int, Query(ge=1, le=100)]


def _serialize_music(info: MusicInfo) -> schemas.MusicInfo:
    """将内部音乐信息转换为 REST 响应模型。"""
    return schemas.MusicInfo(**info.to_dict())


@router.get(
    "/search",
    summary="搜索音乐元数据",
    response_model=list[schemas.MusicInfo],
)
async def search_music(
        query: str = Query(min_length=1),
        count: CountParam = 20,
        _: schemas.TokenPayload = Depends(verify_token),
) -> list[schemas.MusicInfo]:
    """按歌曲、专辑或艺术家关键词搜索标准音乐候选。"""
    results = await MusicChain().async_search(query=query, limit=count)
    return [_serialize_music(info) for info in results]


@router.post(
    "/recognize",
    summary="识别音乐元数据详情",
    response_model=schemas.MusicInfo,
)
async def recognize_music(
        request: schemas.MusicRecognizeRequest,
        _: schemas.TokenPayload = Depends(verify_token),
) -> schemas.MusicInfo:
    """根据音乐元数据来源和媒体 ID 获取标准详情。"""
    info = await MusicChain().async_recognize(
        source=request.source,
        media_id=request.media_id,
    )
    if not info:
        raise HTTPException(status_code=404, detail="未识别到音乐信息")
    return _serialize_music(info)


@router.get(
    "/explore",
    summary="探索热门音乐",
    response_model=list[schemas.MusicInfo],
)
async def explore_music(
        page: Annotated[int, Query(ge=1)] = 1,
        count: CountParam = 30,
        _: schemas.TokenPayload = Depends(verify_token),
) -> list[schemas.MusicInfo]:
    """按月度全站收听榜单分页返回可搜索和订阅的音乐候选。"""
    results = await MusicChain().async_chart(
        range_name="this_month",
        page=page,
        count=count,
    )
    return [_serialize_music(info) for info in results]
