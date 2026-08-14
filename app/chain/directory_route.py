from pathlib import Path

from app import schemas
from app.chain import ChainBase
from app.db.systemconfig_oper import SystemConfigOper
from app.domain.context import MediaInfo
from app.modules.themoviedb.category import CategoryHelper
from app.platform.events import eventmanager
from app.schemas import ConfigChangeEventData
from app.schemas.types import (
    DirectoryMatchMode,
    EventType,
    MediaType,
    SystemConfigKey,
)
from app.services.directory import DirectoryHelper


class DirectoryRouteChain(ChainBase):
    """分类诊断、目录路由预览及其设置的业务编排。"""

    @staticmethod
    def get_settings() -> schemas.DirectoryRouteSettings:
        """从同一缓存快照读取目录配置及候选选择模式。"""
        snapshot = SystemConfigOper().get_many((
            SystemConfigKey.Directories,
            SystemConfigKey.DirectoryMatchMode,
        ))
        directories = snapshot[SystemConfigKey.Directories.value]
        match_mode = snapshot[SystemConfigKey.DirectoryMatchMode.value]
        return schemas.DirectoryRouteSettings(
            directories=directories or [],
            match_mode=DirectoryHelper.get_match_mode(match_mode),
        )

    @staticmethod
    async def save_settings(
            route_settings: schemas.DirectoryRouteSettings,
    ) -> schemas.DirectoryRouteSettings:
        """原子保存目录配置和候选选择模式，并广播一次配置变更。"""
        values = {
            SystemConfigKey.Directories: [
                directory.model_dump(mode="json", exclude_none=True)
                for directory in route_settings.directories
            ] or None,
            SystemConfigKey.DirectoryMatchMode: route_settings.match_mode.value,
        }
        changed_keys = await SystemConfigOper().async_set_many(values)
        if changed_keys:
            await eventmanager.async_send_event(
                etype=EventType.ConfigChanged,
                data=ConfigChangeEventData(
                    key=changed_keys,
                    value={key.value: value for key, value in values.items()},
                    change_type="update",
                ),
            )
        return route_settings

    @staticmethod
    def preview(
            request: schemas.TransferRoutePreviewRequest,
    ) -> schemas.TransferRoutePreviewResponse:
        """
        使用已识别媒体快照预览分类与目录路由，不发起外部识别请求。

        :param request: 路由预览请求
        :return: 分类诊断、当前模式路由及两种模式对比
        """
        category_config = request.category_config or CategoryHelper().load()
        if request.media.type == MediaType.MOVIE:
            category_rules = category_config.movie or {}
        elif request.media.type == MediaType.TV:
            category_rules = category_config.tv or {}
        else:
            category_rules = {}

        category_decision = CategoryHelper.evaluate_category(
            categorys=category_rules,
            tmdb_info=request.metadata,
        )
        provided_category = (request.media.category or "").strip()
        category_decision.provided_category = provided_category
        if provided_category:
            if (
                    category_decision.automatic_category
                    and category_decision.automatic_category != provided_category
            ):
                category_decision.warnings.append(schemas.RouteDiagnosticWarning(
                    code="provided_category_conflict",
                    message="显式类别与自动分类结果不一致，路由使用显式类别",
                ))
            category_decision.selected_category = provided_category
            category_decision.source = "provided"

        media = MediaInfo(
            type=request.media.type,
            title=request.media.title,
            year=request.media.year,
            category=category_decision.selected_category,
        )
        directory_helper = DirectoryHelper()
        directories = (
            directory_helper.get_dirs()
            if request.directories is None
            else request.directories
        )
        effective_mode = directory_helper.get_match_mode(request.match_mode)
        route_kwargs = {
            "media": media,
            "directories": directories,
            "include_unsorted": request.include_unsorted,
            "storage": request.storage,
            "src_path": Path(request.src_path) if request.src_path else None,
            "target_storage": request.target_storage,
            "dest_path": Path(request.dest_path) if request.dest_path else None,
            "valid_categories": list(category_rules.keys()),
        }
        comparisons = [
            directory_helper.evaluate_route(**route_kwargs, match_mode=mode)
            for mode in (
                DirectoryMatchMode.SEQUENTIAL,
                DirectoryMatchMode.SPECIFICITY,
            )
        ]
        route = next(
            decision for decision in comparisons if decision.mode == effective_mode
        )
        return schemas.TransferRoutePreviewResponse(
            media=request.media,
            metadata=request.metadata,
            category=category_decision,
            route=route,
            comparisons=comparisons,
        )
