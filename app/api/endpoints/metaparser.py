"""名称解析环的顺序与启停端点。

顺序即语义，因此列表交出的是裁决后的最终生效顺序而不是用户排的那份原始配置：配置里
没排到的环按声明 priority 追加在末尾，只回显配置看不出它究竟落在哪儿。写入接口按标识
接收整份顺序，标识里含 ``#`` 与 ``@``，走请求体而不是路径参数以免依赖客户端转义。

顺序改的是全局识别行为，因此与其它系统设置一样只对管理员开放。
"""

from typing import Any, List

from fastapi import Body, Depends, HTTPException
from starlette import status

from app.api.deps import get_current_active_superuser, get_current_active_superuser_async
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.metaparser import MetaParserPipelineService
from app.schemas.metaparse import (
    MetaParserOrderEntry as _SchemaMetaParserOrderEntry,
    MetaParserPipeline as _SchemaMetaParserPipeline,
    MetaParserToggle as _SchemaMetaParserToggle,
)

router = ResponseAPIRouter()


@router.get(
    "/pipeline",
    summary="查询名称解析环的生效顺序",
    response_model=_SchemaMetaParserPipeline,
)
def parser_pipeline(
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    列出全部名称解析环及其最终生效顺序

    列表含内建识别环与当前停用的环：前者让用户看得到扩展环接在什么之后，后者让停用的
    环仍能看出排在哪、也改得回来。解析环标识按登记方与声明标识拆成插件、分身、环三段
    随条目一并给出。

    :param _: 鉴权
    :return: 按生效顺序排列的解析环
    """
    return MetaParserPipelineService().list_pipeline()


@router.post(
    "/order",
    summary="保存名称解析环的执行顺序",
    response_model=_SchemaMetaParserPipeline,
)
async def save_parser_order(
    order: List[_SchemaMetaParserOrderEntry] = Body(...),
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """
    按用户排定的次序保存名称解析环的顺序与启停

    :param order: 按目标顺序排列的顺序项
    :param _: 鉴权
    :return: 保存后重新裁决出的生效顺序
    """
    try:
        return await MetaParserPipelineService().save_order(order)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        )


@router.post(
    "/toggle",
    summary="启停单个名称解析环",
    response_model=_SchemaMetaParserPipeline,
)
async def toggle_parser(
    payload: _SchemaMetaParserToggle,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> Any:
    """
    单独启停一个名称解析环

    :param payload: 解析环标识与目标启停状态
    :param _: 鉴权
    :return: 保存后重新裁决出的生效顺序
    """
    try:
        return await MetaParserPipelineService().set_enabled(
            payload.parser, payload.enabled
        )
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        )
