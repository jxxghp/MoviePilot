"""旧插件导入路径使用的只读分类兼容适配器。"""

from collections.abc import Mapping
from typing import Any

from app.application.classification.reference import (
    classification_category_resolver_snapshot,
)
from app.domain.context import MediaInfo
from app.foundation.singleton import WeakSingleton
from app.runtime.log import logger
from app.schemas.category import CategoryConfig
from app.schemas.types import MediaType
from app.sdk.classification import classify_media


class CategoryHelper(metaclass=WeakSingleton):
    """兼容旧插件的只读分类门面，不再读取、创建或写入 category.yaml。"""

    def __init__(self) -> None:
        """创建无状态兼容门面，不触发文件或网络访问。"""

    def init(self) -> None:
        """保留旧插件重载调用；活动策略由宿主统一管理，无需重新加载。"""

    def load(self) -> CategoryConfig:
        """投影当前活动策略的分类路径，供只读旧插件继续展示。"""
        return CategoryConfig(
            movie={name: None for name in self.movie_categorys},
            tv={name: None for name in self.tv_categorys},
        )

    def save(self, config: CategoryConfig) -> bool:
        """拒绝旧 YAML 写入；调用方应改用版本化分类策略 API。"""
        del config
        logger.warning("CategoryHelper.save 已停用，请使用 /api/v1/media/classification/policy 发布版本化策略")
        return False

    @property
    def is_movie_category(self) -> bool:
        """返回活动策略是否包含启用的电影分类。"""
        return bool(self.movie_categorys)

    @property
    def is_tv_category(self) -> bool:
        """返回活动策略是否包含启用的电视剧分类。"""
        return bool(self.tv_categorys)

    @property
    def movie_categorys(self) -> list[str]:
        """返回活动策略中的电影分类路径文本。"""
        return self._category_paths(MediaType.MOVIE)

    @property
    def tv_categorys(self) -> list[str]:
        """返回活动策略中的电视剧分类路径文本。"""
        return self._category_paths(MediaType.TV)

    def get_movie_category(self, tmdb_info: Mapping[str, Any] | None) -> str:
        """使用统一分类服务计算 TMDB 电影的当前目录分类。"""
        return self._classify_tmdb_info(tmdb_info, MediaType.MOVIE)

    def get_tv_category(self, tmdb_info: Mapping[str, Any] | None) -> str:
        """使用统一分类服务计算 TMDB 电视剧的当前目录分类。"""
        return self._classify_tmdb_info(tmdb_info, MediaType.TV)

    @staticmethod
    def _category_paths(media_type: MediaType) -> list[str]:
        """把活动策略的安全路径转换为旧插件可消费的文本列表。"""
        resolver = classification_category_resolver_snapshot()
        return ["/".join(path) for path in resolver.category_paths(media_type) if path]

    @staticmethod
    def _classify_tmdb_info(
        tmdb_info: Mapping[str, Any] | None,
        media_type: MediaType,
    ) -> str:
        """构造规范媒体副本并返回统一分类结果，不修改输入 TMDB 字典。"""
        if not tmdb_info:
            return ""
        payload = dict(tmdb_info)
        payload["media_type"] = media_type
        classified = classify_media(MediaInfo(tmdb_info=payload))
        return str(classified.library_category or "").strip()
