"""命令词来源分层的查询端点。

运行期命令表是内建、插件、单独注册三处来源合并出来的一张平表，合并完就看不出哪条命令
归谁管。插件声明失效时——跨插件同词双方一并作废，或与内建同名却没声明接管意图——用户
只会看到自己敲的命令没反应或执行了别的东西，原因此前只落在服务端日志里。本组端点把
合并前的分层与两类失效原样交出，用户敲了没反应时能从这里知道为什么。

本组端点只读：命令词的归属由插件声明与内建命令表决定，改这里改不了归属，遮蔽内建的
插件命令要靠停用插件、撞车的命令词要靠插件作者改词。

命令表里有 `/restart`、`/clear_cache` 这类系统操作，来源信息会暴露装了哪些插件及其
实例，因此与其它系统设置一样只对管理员开放。
"""

from typing import Any, List

from fastapi import Depends

from app.api.deps import get_current_active_superuser
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.commands import get_command_origins
from app.schemas.command import CommandOrigin as _SchemaCommandOrigin

router = ResponseAPIRouter()


@router.get(
    "/origins",
    summary="查询命令词的来源分层",
    response_model=List[_SchemaCommandOrigin],
)
def command_origins(
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    列出全部命令词的来源分层

    每条给出当前生效定义来自内建、哪个插件实例还是单独注册，以及被压住的下层；插件声明
    因跨插件同词而整体失效、或因与内建同名却未声明接管意图而被拒时一并给出详情。

    :param _: 鉴权
    :return: 按命令词排序的来源条目
    """
    return get_command_origins()


@router.get(
    "/conflicts",
    summary="查询插件声明失效的命令词",
    response_model=List[_SchemaCommandOrigin],
)
def command_conflicts(
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    列出插件声明未能生效的命令词

    含两类：被多个插件同时声明而双方一并作废的，以及与内建命令同名却未声明接管意图而
    被拒的。用户敲某条命令没得到预期结果时先看这里。

    :param _: 鉴权
    :return: 按命令词排序的来源条目，仅含存在失效声明的命令词
    """
    return [
        origin
        for origin in get_command_origins()
        if origin.conflict is not None or origin.declined
    ]
