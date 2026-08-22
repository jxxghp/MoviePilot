"""服务实例的类型目录、配置读写与默认调用目标端点。

下载器、媒体服务器、消息通知、存储与登录认证共用本组端点：各族的类型登记在同一张
表里、配置存在同一张服务实例配置表里，按「能力标签加类型标识」两个维度索引，形状也
完全相同，拆成多个端点只会得到多份同样的代码。

带配置载荷的端点一律要求超级管理员：``config`` 里装着 token、password 与
client_secret，读与写都不是普通用户该碰的。实例选择器端点只交出身份与启用态、不带
``config``，因此按它的消费方——工作流编辑器——同一道门槛收在管理权限上；把它抬到超级
管理员会让有管理权限的用户编辑工作流时选择器永远是空的。登录页那条无鉴权的登录入口
列表取的是「入口描述」、同样不带 ``config``，与本组端点不是同一件事。
"""

from typing import Any, Dict, List, Set

from fastapi import Depends, HTTPException, Query
from starlette import status

from app.api.deps import get_current_active_manage_user, get_current_active_superuser
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.api.service_secrets import mask_secret_values, restore_masked_secrets
from app.application.configuration import get_configured_system_config
from app.application.plugin.runtime import get_plugin_manager as PluginManager
from app.application.service_config import get_configured_service_instance_configs
from app.db.models.serviceconfig import BUILTIN_PROVIDER
from app.db.oper.serviceconfig import ServiceConfigNameConflictError
from app.runtime.extensions.contract.declaration import ServiceInstanceRequirement
from app.runtime.extensions.contract.instance import extension_id_of
from app.runtime.extensions.projection.module_declarations import builtin_multi_instance
from app.runtime.extensions.service_config import service_supports_default_target
from app.runtime.extensions.admission.service_config import (
    service_config_record,
    service_config_record_violation,
)
from app.runtime.extensions.admission.service_instance_requirement import (
    service_instance_candidates,
    service_instance_reference_issue,
)
from app.runtime.extensions.registry.service_family import service_family_registry
from app.runtime.extensions.registry.service_instance import service_instance_registry
from app.schemas.response import Response as _SchemaResponse
from app.schemas.service import ServiceConfigForm as _SchemaServiceConfigForm
from app.schemas.service import (
    ServiceConfigProviderIssue as _SchemaServiceConfigProviderIssue,
)
from app.schemas.service import ServiceFamilyInfo as _SchemaServiceFamilyInfo
from app.schemas.service import (
    ServiceInstanceConfigInfo as _SchemaServiceInstanceConfigInfo,
)
from app.schemas.service import (
    ServiceInstanceConfigPayload as _SchemaServiceInstanceConfigPayload,
)
from app.schemas.service import (
    ServiceInstanceSelection as _SchemaServiceInstanceSelection,
)
from app.schemas.service import ServiceTypeInfo as _SchemaServiceTypeInfo
from app.schemas.types import SystemConfigKey

router = ResponseAPIRouter()

# 「提供方已消失」的三种成因代码，三者的处置动作各不相同
PROVIDER_NOT_INSTALLED = "not_installed"
PROVIDER_DISABLED = "disabled"
PROVIDER_START_FAILED = "start_failed"


@router.get(
    "/config_form/{capability}/{service_type}",
    summary="获取服务实例类型的专属配置界面",
    response_model=_SchemaServiceConfigForm,
)
def config_form(
    capability: str,
    service_type: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    按能力标签与类型标识获取扩展为该类型声明的配置界面

    下载器、媒体服务器、消息通知、存储与登录认证共用本端点：各族的服务类型登记在同一
    张表里，按「能力标签加类型标识」两个维度索引，界面形状也完全相同，拆成多个端点只会
    得到多份同样的代码。界面归属声明该类型的扩展本身，不归属某个插件：同一
    插件可能同时声明服务实例与另一种能力，此处只按类型索引到登记时随声明附带
    的界面，不会读到扩展自身的 get_form()。

    未登记的类型返回 ``available`` 为 False 而非报错：内建类型登记在各内建模块的
    capability.toml 里而不在本表中，本端点没有可用的全量类型目录，无法区分「类型
    不存在」与「类型没有专属界面」；把二者一并答成「没有专属界面」是此处唯一诚实
    的回答，前端据此沿用内建渲染方式。

    本端点同时下发 ``multi_instance``：前端要决定该类型的配置列表上要不要给出
    新增第二份的入口。扩展声明的类型读登记表，内建类型读各自 `capability.toml`
    的声明——本地存储只有一个文件系统，第二份配置指的仍是同一个盘，它在清单里
    声明为单实例，前端据此不给出新增入口。两处都问不到时答 True，与声明缺省即
    多实例的口径一致。

    ``config_schema`` 与 ``available`` 各自独立下发：契约描述配置形状，界面描述
    配置呈现，声明方可以只给其一。只声明契约的类型 ``available`` 仍为 False，
    前端据契约生成默认表单。
    :param capability: 能力标签，取值为宿主已登记的服务族，宿主自带 downloader、
        mediaserver、notification、storage、auth
    :param service_type: 类型标识，即该族配置模型的 type
    :param _: 鉴权
    :return: available 为 False 时该类型没有专属界面，界面相关字段均为 None
    """
    if not service_family_registry.is_registered(capability):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"服务能力 {capability} 不支持声明服务实例",
        )
    declared = builtin_multi_instance(capability, service_type)
    empty = {
        "available": False,
        "name": None,
        "multi_instance": True if declared is None else declared,
        "conf": None,
        "model": None,
        "component": None,
        "remote": None,
        "config_schema": None,
    }
    entry = service_instance_registry.find(capability, service_type)
    if entry is None:
        return empty
    empty["name"] = entry.name
    empty["multi_instance"] = entry.multi_instance
    empty["config_schema"] = entry.config_schema
    if entry.config_form is not None:
        layout, defaults = entry.config_form
        return {**empty, "available": True, "conf": layout, "model": defaults}
    if entry.config_component is not None:
        return {
            **empty,
            "available": True,
            "component": entry.config_component.get("component"),
            "remote": entry.config_component.get("remote"),
        }
    return empty


def _require_family(capability: str) -> None:
    """
    校验能力标签是已登记的服务族

    :param capability: 能力标签
    :return: 无返回值
    :raises HTTPException: 该标签不是已登记的服务族
    """
    if not service_family_registry.is_registered(capability):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"服务能力 {capability} 不支持声明服务实例",
        )


def _config_info(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    把一条配置行整理成下发形状，凭据换成掩码

    类型是否可用按服务实例登记表当下有没有这个「能力标签加类型标识」判定，与 ``provider``
    列无关：那一列只是记账，用来在类型查不到时说出是哪个扩展没在场。

    :param row: 服务实例配置行
    :return: 配置的下发形状
    """
    entry = service_instance_registry.find(row["capability"], row["type"])
    config, config_paths = mask_secret_values(row.get("config") or {}, "config")
    host_config, host_paths = mask_secret_values(row.get("host_config") or {}, "host_config")
    return {
        "capability": row["capability"],
        "type": row["type"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "config": config,
        "host_config": host_config,
        "is_default_target": bool(row["is_default_target"]),
        "provider": row["provider"],
        "masked_fields": [*config_paths, *host_paths],
        "type_available": entry is not None,
        "type_name": entry.name if entry else None,
    }


def _shaped_record(capability: str, conf: Dict[str, Any]) -> Dict[str, Any]:
    """
    按类型声明的契约判定一条待写入的服务配置，并整形为配置行

    判定与实例构造路径共用同一个函数，因此不会出现「写得进去、用不起来」的分歧；
    畸形配置在落盘前退回并说明是哪个字段有问题，而不是等到构造实例时才静默跳过。

    :param capability: 能力标签
    :param conf: 单条服务配置
    :return: 服务实例配置表的行
    :raises HTTPException: 配置不合契约，或缺少类型标识与实例名
    """
    violation = service_config_record_violation(capability, conf, 0)
    if violation:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=violation)
    record = service_config_record(capability, conf)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="服务实例配置缺少类型标识或实例名",
        )
    return record


def _absent_provider_reason(provider: str, installed: Set[str]) -> str:
    """
    判定提供方缺席的成因

    三种成因的处置动作各不相同：装回插件、启用插件、以及查插件日志。最后一种最难自查
    ——插件确实在跑、也确实是启用状态，只是它声明的这个服务类型没能登记上，用户看到的
    是配置好好地列着而实例静默不存在。

    :param provider: 记账中提供该类型的扩展实例键
    :param installed: 当前已安装的扩展标识集合
    :return: 成因代码
    """
    if extension_id_of(provider) not in installed:
        return PROVIDER_NOT_INSTALLED
    if not PluginManager().get_plugin_state(provider):
        return PROVIDER_DISABLED
    return PROVIDER_START_FAILED


@router.get(
    "/families",
    summary="获取已登记的服务族",
    response_model=List[_SchemaServiceFamilyInfo],
)
def service_families(_: ApiPrincipal = Depends(get_current_active_superuser)) -> Any:
    """
    列出当前已登记的全部服务族

    族是登记出来的而不是写死的枚举：扩展可以带进新的族，宿主没有理由预先穷举。前端
    据本端点决定配置页上出现哪几族，而不是照着一份硬编码列表渲染。

    :param _: 鉴权
    :return: 按能力标签升序排列的服务族列表
    """
    return [
        {
            "capability": entry.capability,
            "name": entry.name,
            "distribution": entry.distribution.value,
            "owner": entry.owner,
        }
        for entry in service_family_registry.entries()
    ]


@router.get(
    "/types/{capability}",
    summary="获取某族可新增配置的服务实例类型",
    response_model=List[_SchemaServiceTypeInfo],
)
def service_types(
    capability: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    列出某族当前已登记的服务实例类型，供前端渲染新增配置的下拉框

    只列扩展声明的类型：内建类型登记在各内建模块的 capability.toml 里而不在本表中，
    宿主没有一份跨两处的全量目录。前端把本端点的结果与内建类型列表并起来即得完整
    下拉框，这与配置界面端点「未登记类型答无专属界面」的口径一致。

    ``multi_instance`` 决定要不要给出新增第二份的入口；``config_form_available``
    决定用登记方自带的界面还是按 ``config_schema`` 生成默认表单，界面内容本身由
    配置界面端点按类型单取，不随目录整批下发——一份目录里塞进十几棵组件树，前端
    多半一棵都用不上。

    :param capability: 能力标签
    :param _: 鉴权
    :return: 该族已登记的类型列表，按登记顺序排列
    """
    _require_family(capability)
    return [
        {
            "capability": adapter.entry.capability,
            "type": adapter.entry.service_type,
            "name": adapter.entry.name,
            "icon": adapter.entry.icon,
            "multi_instance": adapter.entry.multi_instance,
            "config_form_available": (
                adapter.entry.config_form is not None
                or adapter.entry.config_component is not None
            ),
            "config_schema": adapter.entry.config_schema,
            "provider": adapter.entry.owner,
            "distribution": adapter.entry.distribution.value,
        }
        for adapter in service_instance_registry.adapters(capability)
    ]


@router.get(
    "/configs/{capability}",
    summary="获取某族的全部实例配置",
    response_model=List[_SchemaServiceInstanceConfigInfo],
)
def list_service_configs(
    capability: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    列出某族的全部实例配置，凭据一律以掩码下发

    ``config`` 列里装着 token、password 与 client_secret，明文随列表下发会落进前端
    内存、日志与浏览器缓存，因此按键名识别出的凭据一律换成掩码，被掩码的字段路径列在
    ``masked_fields`` 里。编辑时把掩码原样回传即表示该项未改动，改一个端口号不必重新
    输入密码。

    :param capability: 能力标签
    :param _: 鉴权
    :return: 该族全部实例配置，按写入先后排列
    """
    _require_family(capability)
    return [
        _config_info(row)
        for row in get_configured_service_instance_configs().list_rows(capability)
    ]


@router.post(
    "/configs/{capability}",
    summary="新增一条服务实例配置",
    response_model=_SchemaServiceInstanceConfigInfo,
)
def create_service_config(
    capability: str,
    payload: _SchemaServiceInstanceConfigPayload,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    新增一条服务实例配置

    新增的实例一律不是默认调用目标，该置位由专用入口显式设定。同族同类型下重名由唯一
    约束把关，冲突以 409 退回并说明该换哪个名字，而不是把数据库异常抛到界面。

    :param capability: 能力标签
    :param payload: 配置载荷，形状与该族配置模型一致
    :param _: 鉴权
    :return: 新增后的配置，凭据已掩码
    """
    _require_family(capability)
    conf = restore_masked_secrets(payload.model_dump(exclude_none=True), {})
    record = _shaped_record(capability, conf)
    try:
        created = get_configured_service_instance_configs().create_record(capability, record)
    except ServiceConfigNameConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return _config_info(created)


@router.put(
    "/configs/{capability}/{service_type}",
    summary="更新一条服务实例配置",
    response_model=_SchemaServiceInstanceConfigInfo,
)
def update_service_config(
    capability: str,
    service_type: str,
    payload: _SchemaServiceInstanceConfigPayload,
    name: str = Query(description="待更新配置的实例名"),
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    更新一条服务实例配置

    只改这一行：更新语句按「能力标签加类型加实例名」定位，既不读整族也不回写整族，
    因此两位管理员同时改不同的配置不会互相覆盖，而整份列表替换做不到这一点。

    实例名走查询参数而不是路径段：它是用户自填的，可以带斜杠与空格，落进路径段会被
    路由切断。类型标识是声明面上的标识符，没有这个问题。

    凭据回传掩码即表示未改动，服务端从库里取回原值；回传其它内容即按新值落库。其余
    字段是整条替换，未提交的按缺省取值落库。

    :param capability: 能力标签
    :param service_type: 类型标识
    :param payload: 配置载荷，形状与该族配置模型一致
    :param name: 待更新配置的实例名
    :param _: 鉴权
    :return: 更新后的配置，凭据已掩码
    """
    _require_family(capability)
    service = get_configured_service_instance_configs()
    stored = service.get_row(capability, service_type, name)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{capability} 类型 {service_type} 下不存在名为 {name} 的配置",
        )
    # 宿主消费的实例级字段在配置形状里是平铺的，与 config 一并按库中原值回填掩码
    reference = {**(stored.get("host_config") or {}), "config": stored.get("config") or {}}
    conf = restore_masked_secrets(payload.model_dump(exclude_none=True), reference)
    conf["type"] = service_type
    if not conf.get("name"):
        conf["name"] = name
    record = _shaped_record(capability, conf)
    update_payload = {
        "name": record["name"],
        "enabled": record["enabled"],
        "config": record["config"],
        "host_config": record["host_config"],
    }
    # 登记表查不到该类型时不改写记账：把 provider 抹掉，「提供方已消失」这条提示
    # 就再也筛不出这一行，而这正是加那一列的目的
    if record["provider"]:
        update_payload["provider"] = record["provider"]
    try:
        service.update_record(capability, service_type, name, update_payload)
    except ServiceConfigNameConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    # 读到写之间这一行可能已被另一请求删掉，此时更新语句一行都没命中，据实答 404
    updated = service.get_row(capability, service_type, record["name"])
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{capability} 类型 {service_type} 下不存在名为 {name} 的配置",
        )
    return _config_info(updated)


@router.delete(
    "/configs/{capability}/{service_type}",
    summary="删除一条服务实例配置",
    response_model=_SchemaResponse[None],
)
def delete_service_config(
    capability: str,
    service_type: str,
    name: str = Query(description="待删除配置的实例名"),
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    删除一条服务实例配置，同族其余配置不受影响

    :param capability: 能力标签
    :param service_type: 类型标识
    :param name: 待删除配置的实例名
    :param _: 鉴权
    :return: 删除结果
    """
    _require_family(capability)
    if not get_configured_service_instance_configs().delete_record(
        capability, service_type, name
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{capability} 类型 {service_type} 下不存在名为 {name} 的配置",
        )
    return _SchemaResponse(success=True)


@router.put(
    "/default_target/{capability}/{service_type}",
    summary="设为某族的默认调用目标",
    response_model=_SchemaResponse[None],
)
def set_service_default_target(
    capability: str,
    service_type: str,
    name: str = Query(description="待置位配置的实例名"),
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    把指定实例设为该族的默认调用目标，即外部调用未指定实例时选中的那一行

    同一事务内先清除该族原有置位再置位新目标；目标实例没有配置行时以 404 退回且原有
    置位保持不变——先清后置一旦在目标缺席时执行到一半，该族会从「有默认调用目标」变成
    「没有」，而调用方只看到一个失败返回值。

    没有默认调用目标的族以 400 退回：登录认证族里用户点的永远是具体某个入口，不存在
    「调用未指定实例」这回事，标记本身无从解释。

    :param capability: 能力标签
    :param service_type: 类型标识
    :param name: 待置位配置的实例名
    :param _: 鉴权
    :return: 置位结果
    """
    _require_family(capability)
    if not service_supports_default_target(capability):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"服务能力 {capability} 没有默认调用目标",
        )
    if not get_configured_service_instance_configs().set_default_target(
        capability, service_type, name
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{capability} 类型 {service_type} 下不存在名为 {name} 的配置",
        )
    return _SchemaResponse(success=True)


@router.delete(
    "/default_target/{capability}",
    summary="清除某族的默认调用目标",
    response_model=_SchemaResponse[None],
)
def clear_service_default_target(
    capability: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    清除该族的默认调用目标置位，清除后该族不再有默认调用目标

    置位是族级的、每族至多一个，因此清除不必指到某一条配置。本来就没有置位时同样
    返回成功，重复清除是空操作。

    :param capability: 能力标签
    :param _: 鉴权
    :return: 清除结果
    """
    _require_family(capability)
    get_configured_service_instance_configs().clear_default_target(capability)
    return _SchemaResponse(success=True)


@router.get(
    "/instance_candidates/{capability}",
    summary="获取某族可供扩展点选择的服务实例",
    response_model=_SchemaServiceInstanceSelection,
)
def service_instance_selection(
    capability: str,
    types: List[str] = Query(default=[], description="收窄到的类型标识，可重复给出"),
    selected: str = Query(default="", description="已选实例名，给出即一并判定它是否仍然可用"),
    _: ApiPrincipal = Depends(get_current_active_manage_user),
) -> Any:
    """
    列出声明了作用对象的动作或仪表盘当前可选的服务实例，并判定已选实例的现状

    未登记的族答 ``family_registered`` 为 False 而不是报错：提供该族的扩展不在场与
    用户一份配置都还没建，是两种处置动作完全不同的处境——装回扩展，还是去设置页新建。
    一并答成 404 会让前端只能显示同一句「取不到」。

    候选只带身份与启用态，不带 ``config``：这份列表是给用户挑选用的，而配置载荷里
    装着凭据，随选择器下发即等于把它们摊给每一个能编辑工作流的人。

    ``issue`` 只在给了 ``selected`` 时才可能非空，取值是稳定的成因代码，措辞由前端
    渲染。它与「提供方已消失」那条通路方向相反：那条回答「配置还在、提供方没了」，
    本字段回答「引用还在、被引用的实例没了」。

    :param capability: 能力标签
    :param types: 收窄到的类型标识，留空表示该族任意类型都可选
    :param selected: 已选实例名，留空表示尚未选择
    :param _: 鉴权
    :return: 该族当前的可选实例与已选实例的现状
    """
    requirement = ServiceInstanceRequirement(capability=capability, types=tuple(types))
    registered = service_family_registry.is_registered(capability)
    candidates = service_instance_candidates(requirement) if registered else ()
    return {
        "capability": capability,
        "family_registered": registered,
        "supports_default_target": service_supports_default_target(capability),
        "candidates": [
            {
                "type": item.type,
                "name": item.name,
                "enabled": item.enabled,
                "is_default_target": item.is_default_target,
            }
            for item in candidates
        ],
        "selected": selected or None,
        "issue": service_instance_reference_issue(requirement, selected or None),
    }


@router.get(
    "/absent_providers",
    summary="获取提供方已消失的服务实例配置",
    response_model=List[_SchemaServiceConfigProviderIssue],
)
def absent_service_providers(
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> Any:
    """
    列出提供方已不在场的服务实例配置，并分开三种成因

    判据是服务实例登记表当下有没有这个「能力标签加类型标识」，而不是 ``provider`` 列
    在不在场：后者只是记账，让它参与判定会造出第二个事实源，两边一漂移就是静默错误。
    也正因如此，「插件在跑、类型却没登记上」这种最难自查的情形才筛得出来——若按记账
    判定，这一行的提供方明明在场，会被整个漏掉。

    内建类型不参与：它们登记在各内建模块的 capability.toml 里而不在本表中，按本表判
    定会把每一条内建配置都算成提供方已消失。

    :param _: 鉴权
    :return: 提供方已消失的配置列表，按族与写入先后排列
    """
    installed = set(
        get_configured_system_config().get(SystemConfigKey.UserInstalledPlugins) or []
    )
    service = get_configured_service_instance_configs()
    issues: List[Dict[str, Any]] = []
    for capability in service_family_registry.capabilities():
        for row in service.list_rows(capability):
            provider = row["provider"]
            if provider == BUILTIN_PROVIDER:
                continue
            if service_instance_registry.find(capability, row["type"]) is not None:
                continue
            issues.append({
                "capability": capability,
                "type": row["type"],
                "name": row["name"],
                "provider": provider,
                "extension_id": extension_id_of(provider),
                "reason": _absent_provider_reason(provider, installed),
            })
    return issues
