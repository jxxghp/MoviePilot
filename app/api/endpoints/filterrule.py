"""筛选规则来源分层的查询端点。

运行期规则集是内置 < 插件 < 用户三层合并出来的一张平表，合并完就看不出哪条来自哪里。
本组端点把合并前的分层原样交出，用户据此知道一条规则归谁管；跨插件同名而整体失效的
标识也一并交出，那件事此前只在日志里留过一次告警。

本组端点只读：单独禁用某个插件的规则会成为内置、插件、用户之外的第四层，而用户自定义
那一层已经能做同一件事且更彻底——同名的用户规则永远赢，可以替换而不只是抹掉，且不会让
引用该标识的规则组落到「规则不存在」。规则来源在此可见之后，用户知道该覆盖哪一条。

只读，改的仍是既有的自定义规则与规则组配置项，因此与其它系统设置一样只对管理员开放。
"""

from typing import Any, List

from fastapi import Depends

from app.api.deps import get_current_active_superuser
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.rules import FilterRuleOriginService
from app.schemas.rule import FilterRuleOrigin as _SchemaFilterRuleOrigin

router = ResponseAPIRouter()


@router.get(
    "/rules",
    summary="查询筛选规则的来源分层",
    response_model=List[_SchemaFilterRuleOrigin],
)
def rule_origins(
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    列出全部筛选规则标识的来源分层

    每条给出当前生效定义来自内置、哪个插件实例还是用户自定义，以及被压住的下层；插件
    声明因跨插件同名而整体失效时一并给出涉及的插件。

    :param _: 鉴权
    :return: 按规则标识排序的来源条目
    """
    return FilterRuleOriginService().list_rule_origins()


@router.get(
    "/groups",
    summary="查询筛选规则组的来源分层",
    response_model=List[_SchemaFilterRuleOrigin],
)
def rule_group_origins(
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    列出全部筛选规则组名的来源分层

    :param _: 鉴权
    :return: 按规则组名排序的来源条目
    """
    return FilterRuleOriginService().list_rule_group_origins()


@router.get(
    "/conflicts",
    summary="查询因跨插件同名而失效的筛选规则标识",
    response_model=List[_SchemaFilterRuleOrigin],
)
def rule_conflicts(
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    列出插件声明因跨插件同名而整体失效的规则标识与规则组名

    :param _: 鉴权
    :return: 按种类与标识排序的来源条目，仅含存在冲突的标识
    """
    return FilterRuleOriginService().list_conflicts()
