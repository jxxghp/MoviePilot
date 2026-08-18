from typing import Any, Optional

from app.application.configuration import get_configured_system_config
from app.schemas.types import SystemConfigKey


class RecognitionRuleService:
    """集中读取用户持久化的媒体识别规则，供启动层注入纯领域匹配器。"""

    def __init__(self, systemconfig: Optional[Any] = None) -> None:
        """绑定系统配置访问器，测试可传入隔离替身。"""
        self._systemconfig = systemconfig

    def _config(self) -> Any:
        """惰性获取配置服务，避免引导阶段早于组合根装配。"""
        return self._systemconfig or get_configured_system_config()

    def get_customization(self) -> object:
        """返回当前自定义占位符配置。"""
        return self._config().get(SystemConfigKey.Customization)

    def get_release_groups(self) -> object:
        """返回当前用户自定义制作组配置。"""
        return self._config().get(SystemConfigKey.CustomReleaseGroups)

    def get_custom_words(self) -> object:
        """返回当前自定义识别词配置。"""
        return self._config().get(SystemConfigKey.CustomIdentifiers)
