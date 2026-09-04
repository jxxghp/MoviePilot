"""多媒体分类策略、字段目录、预览、影响和版本管理 API。"""

from __future__ import annotations

from enum import Enum
from functools import partial
from typing import cast

from fastapi import Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.api.context import (
    get_async_session,
    get_classification_runtime,
    get_host_runtime,
)
from app.api.dependencies.auth import (
    get_current_active_superuser_async,
    get_current_active_user_async,
)
from app.api.response import ResponseAPIRouter
from app.application.classification.analysis import (
    ClassificationAnalysisService,
    RecentHistoryClassificationSampleProvider,
)
from app.application.classification.configuration import (
    ClassificationPolicyNotInitializedError,
    ClassificationPolicyRevisionNotFoundError,
    ClassificationPolicyValidationError,
)
from app.application.classification.contract import (
    ClassificationPolicyConflictError,
    ClassificationPolicyStateCorruptError,
)
from app.application.classification.execution import ClassificationExecutionPort
from app.application.classification.runtime import ClassificationRuntime
from app.application.history import DownloadHistoryQueryPort
from app.chain.media import MediaChain
from app.schemas.category import (
    ClassificationEvaluation,
    ClassificationFacts,
    ClassificationFieldCatalog,
    ClassificationImpactAnalysis,
    ClassificationImpactRequest,
    ClassificationPolicy,
    ClassificationPolicyHistory,
    ClassificationPolicyPublishRequest,
    ClassificationPolicyRollbackRequest,
    ClassificationPolicyRollbackResult,
    ClassificationPolicyValidateRequest,
    ClassificationPreviewRequest,
    ClassificationRevisionConflict,
    ClassificationValidationResult,
)
from app.schemas.response import Response
from app.schemas.types import MediaSource, MediaType
from app.startup.composition.context import HostRuntime

router = ResponseAPIRouter()

_STRUCTURED_WRITE_RESPONSES = {
    status.HTTP_409_CONFLICT: {
        "model": Response[ClassificationRevisionConflict],
        "description": "分类策略 revision 冲突",
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": Response[ClassificationValidationResult],
        "description": "分类策略领域校验失败",
    },
}


def _get_analysis_service(
    db: object = Depends(get_async_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> ClassificationAnalysisService:
    """组装分类分析服务及其只读近期历史样本端口。"""
    return ClassificationAnalysisService(
        runtime.classification.service,
        sample_provider=RecentHistoryClassificationSampleProvider(
            download_history=cast(
                DownloadHistoryQueryPort,
                runtime.history.download_repository(db),
            ),
            transfer_history=runtime.history.transfer_repository,
            facts_resolver=partial(
                _resolve_history_facts,
                runtime.classification_execution,
            ),
        ),
    )


async def _resolve_history_facts(
    execution: ClassificationExecutionPort,
    history: object,
) -> ClassificationFacts | None:
    """按历史记录中的来源和编号重新读取完整媒体信息。"""
    media_source = _enum_text(getattr(history, "media_source", None))
    media_id = str(getattr(history, "media_id", None) or "").strip()
    media_type = _history_media_type(getattr(history, "type", None))
    if not media_source or not media_id or media_type is None:
        return None
    try:
        source = MediaSource(media_source)
        media = await MediaChain().async_recognize_media(
            media_source=source,
            media_id=media_id,
            mtype=media_type,
            music_type=str(getattr(history, "music_type", None) or "").strip() or None,
        )
        if media is None:
            return None
        return await execution.async_build_facts(media)
    except (TypeError, ValueError):
        return None


def _history_media_type(value: object) -> MediaType | None:
    """兼容历史记录中的中文和英文媒体类型。"""
    normalized = _enum_text(value).casefold()
    aliases = {
        "电影": MediaType.MOVIE,
        "movie": MediaType.MOVIE,
        "电视剧": MediaType.TV,
        "tv": MediaType.TV,
        "电视": MediaType.TV,
        "音乐": MediaType.MUSIC,
        "music": MediaType.MUSIC,
    }
    return aliases.get(normalized)


def _enum_text(value: object) -> str:
    """把枚举或普通值转换为去除首尾空白的文本。"""
    if isinstance(value, Enum):
        value = value.value
    return str(value or "").strip()


def _require_active_policy(runtime: ClassificationRuntime) -> ClassificationPolicy:
    """读取活动策略；启动迁移失败时映射为可诊断的 503。"""
    try:
        return runtime.require_policy()
    except ClassificationPolicyNotInitializedError as error:
        diagnostics = runtime.diagnostics()
        message = "；".join(issue.message for issue in diagnostics) or str(error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=message,
        ) from error


def _conflict_response(error: ClassificationPolicyConflictError) -> JSONResponse:
    """生成前端可直接读取 expected/current revision 的 409 响应。"""
    detail = ClassificationRevisionConflict(
        expected_revision=error.expected_revision,
        current_revision=error.current_revision,
    )
    payload = Response[ClassificationRevisionConflict](
        success=False,
        message="分类策略已被其他会话更新，请重新加载或合并后再发布",
        data=detail,
    )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=payload.model_dump(mode="json"),
        headers={
            "X-Classification-Current-Revision": str(error.current_revision),
        },
    )


def _validation_response(
    error: ClassificationPolicyValidationError,
) -> JSONResponse:
    """生成保留完整字段路径和问题代码的 422 策略校验响应。"""
    result = cast(
        ClassificationValidationResult,
        error.result.model_copy(deep=True),
    )
    payload = Response[ClassificationValidationResult](
        success=False,
        message="分类策略校验失败",
        data=result,
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=payload.model_dump(mode="json"),
    )


@router.get(  # type: ignore[misc]
    "/policy",
    summary="读取当前媒体分类策略",
    response_model=ClassificationPolicy,
)
async def get_policy(
    _: object = Depends(get_current_active_user_async),
    runtime: ClassificationRuntime = Depends(get_classification_runtime),
) -> ClassificationPolicy:
    """返回与运行时内部引用隔离的活动策略和 revision。"""
    return _require_active_policy(runtime)


@router.put(  # type: ignore[misc]
    "/policy",
    summary="校验并发布媒体分类策略",
    response_model=ClassificationPolicy,
    responses=_STRUCTURED_WRITE_RESPONSES,
)
async def publish_policy(
    request: ClassificationPolicyPublishRequest,
    _: object = Depends(get_current_active_superuser_async),
    runtime: ClassificationRuntime = Depends(get_classification_runtime),
) -> ClassificationPolicy | JSONResponse:
    """以 CAS revision 发布完整策略，冲突和领域错误返回结构化数据。"""
    try:
        return await runtime.publish_policy(
            request.policy,
            expected_revision=request.expected_revision,
        )
    except ClassificationPolicyConflictError as error:
        return _conflict_response(error)
    except ClassificationPolicyValidationError as error:
        return _validation_response(error)
    except ClassificationPolicyStateCorruptError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@router.get(  # type: ignore[misc]
    "/fields",
    summary="读取媒体分类字段能力目录",
    response_model=ClassificationFieldCatalog,
)
async def get_fields(
    _: object = Depends(get_current_active_user_async),
    runtime: ClassificationRuntime = Depends(get_classification_runtime),
) -> ClassificationFieldCatalog:
    """返回标准字段、动态扩展字段和服务端结构限制。"""
    return ClassificationAnalysisService(runtime.service).fields()


@router.post(  # type: ignore[misc]
    "/validate",
    summary="校验媒体分类策略草稿",
    response_model=ClassificationValidationResult,
)
async def validate_policy(
    request: ClassificationPolicyValidateRequest,
    _: object = Depends(get_current_active_superuser_async),
    runtime: ClassificationRuntime = Depends(get_classification_runtime),
) -> ClassificationValidationResult:
    """执行与发布相同的结构和语义校验，但不修改 revision 或历史。"""
    return ClassificationAnalysisService(runtime.service).validate(request.policy)


@router.post(  # type: ignore[misc]
    "/preview",
    summary="预览媒体分类策略命中过程",
    response_model=ClassificationEvaluation,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": Response[ClassificationValidationResult],
            "description": "预览草稿校验失败",
        }
    },
)
async def preview_policy(
    request: ClassificationPreviewRequest,
    _: object = Depends(get_current_active_user_async),
    runtime: ClassificationRuntime = Depends(get_classification_runtime),
) -> ClassificationEvaluation | JSONResponse:
    """对选择的媒体信息或兼容事实执行策略，并返回完整匹配说明。"""
    if request.policy is None:
        _require_active_policy(runtime)
    try:
        return ClassificationAnalysisService(runtime.service).preview(request)
    except ClassificationPolicyValidationError as error:
        return _validation_response(error)


@router.post(  # type: ignore[misc]
    "/impact",
    summary="分析分类策略对近期样本的估算影响",
    response_model=ClassificationImpactAnalysis,
    responses=_STRUCTURED_WRITE_RESPONSES,
)
async def analyze_impact(
    request: ClassificationImpactRequest,
    _: object = Depends(get_current_active_superuser_async),
    service: ClassificationAnalysisService = Depends(_get_analysis_service),
) -> ClassificationImpactAnalysis | JSONResponse:
    """读取近期历史对应的完整媒体详情后比较策略，不修改媒体或历史数据。"""
    try:
        return await service.impact(
            request.policy,
            expected_revision=request.expected_revision,
            sample_limit=request.sample_limit,
            example_limit=request.example_limit,
            samples=request.samples,
        )
    except ClassificationPolicyConflictError as error:
        return _conflict_response(error)
    except ClassificationPolicyValidationError as error:
        return _validation_response(error)
    except ClassificationPolicyNotInitializedError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@router.get(  # type: ignore[misc]
    "/history",
    summary="读取媒体分类策略历史",
    response_model=ClassificationPolicyHistory,
)
async def get_history(
    _: object = Depends(get_current_active_superuser_async),
    runtime: ClassificationRuntime = Depends(get_classification_runtime),
) -> ClassificationPolicyHistory:
    """返回当前 revision 以及有界、按新到旧排列的历史完整快照。"""
    active = _require_active_policy(runtime)
    return ClassificationPolicyHistory(
        active_revision=active.revision,
        items=list(runtime.service.history()),
    )


@router.post(  # type: ignore[misc]
    "/rollback/{revision}",
    summary="把历史媒体分类策略发布为新版本",
    response_model=ClassificationPolicyRollbackResult,
    responses=_STRUCTURED_WRITE_RESPONSES,
)
async def rollback_policy(
    revision: int,
    request: ClassificationPolicyRollbackRequest,
    _: object = Depends(get_current_active_superuser_async),
    runtime: ClassificationRuntime = Depends(get_classification_runtime),
) -> ClassificationPolicyRollbackResult | JSONResponse:
    """选择历史内容并发布新的单调 revision，不覆写历史版本。"""
    _require_active_policy(runtime)
    try:
        policy = await runtime.service.async_rollback(
            revision,
            expected_revision=request.expected_revision,
        )
    except ClassificationPolicyConflictError as error:
        return _conflict_response(error)
    except ClassificationPolicyValidationError as error:
        return _validation_response(error)
    except ClassificationPolicyRevisionNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    return ClassificationPolicyRollbackResult(
        restored_from_revision=revision,
        policy=policy,
    )
