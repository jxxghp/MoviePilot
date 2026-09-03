"""多媒体分类策略、字段目录、预览、影响和版本管理 API。"""

from __future__ import annotations

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
from app.application.classification.runtime import ClassificationRuntime
from app.application.history import DownloadHistoryQueryPort
from app.schemas.category import (
    ClassificationEvaluation,
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
        ),
    )


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
    """对显式标准事实执行活动策略或未发布草稿并返回完整 trace。"""
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
    """比较活动策略与草稿；样本有限且不触发联网识别或任何写入。"""
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
