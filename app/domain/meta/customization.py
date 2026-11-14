"""
管理标题识别中的自定义占位符及其匹配规则。
"""

from collections.abc import Callable, Iterable

import regex as re

from app.foundation.singleton import Singleton


def _empty_customization() -> tuple[str, ...]:
    """
    启动装配前不提供自定义占位符。
    """
    return ()


_customization_provider: Callable[[], Iterable[str] | None] = _empty_customization


def configure_customization_provider(provider: Callable[[], Iterable[str] | None]) -> None:
    """
    注入当前自定义占位符来源，避免领域匹配器读取持久化配置。
    """
    global _customization_provider
    _customization_provider = provider


def get_customization() -> Iterable[str] | None:
    """
    返回当前自定义占位符原始配置。
    """
    return _customization_provider()


def set_custom_separator(separator: str | None) -> None:
    """
    设置自定义占位符输出分隔符；传入空值恢复宿主默认行为。
    """
    if separator == "":
        separator = None
    if separator is not None and not isinstance(separator, str):
        raise TypeError("custom separator must be a string or None")
    CustomizationMatcher().custom_separator = separator


class CustomizationMatcher(metaclass=Singleton):
    """
    识别自定义占位符。
    """

    def __init__(self) -> None:
        """
        初始化自定义占位符正则缓存。
        """
        self.customization: str | None = None
        self.custom_separator: str | None = None
        self._customization_re_cache: dict[str, re.Pattern[str]] = {}

    @staticmethod
    def normalize_customization(customization: Iterable[object] | None) -> list[object]:
        """
        规范化自定义占位符配置，兼容历史字符串与列表两种保存格式。
        """
        if isinstance(customization, str):
            customization = customization.replace("\n", ";").replace("|", ";").strip(";").split(";")
        if not customization:
            return []
        return list(filter(None, customization))

    @staticmethod
    def _normalize_customization(customization: Iterable[object] | None) -> list[object]:
        """
        兼容旧调用，统一转到公开的自定义占位符规范化入口。
        """
        return CustomizationMatcher.normalize_customization(customization)

    def match(self, title: str | None = None) -> str:
        """
        按当前配置匹配标题中的自定义占位符。

        :param title: 资源标题或文件名；为空时返回空字符串
        :return: 按配置顺序去重后拼接的占位符文本
        """
        if not title:
            return ""
        # 自定义占位符需要跟随系统配置实时生效，避免单例缓存导致保存后仍沿用旧规则。
        customization = self.normalize_customization(
            get_customization()
        )
        if not customization:
            self.customization = None
            return ""
        self.customization = "|".join([f"({item})" for item in customization])

        customization_re = self._customization_re_cache.get(self.customization)
        if not customization_re:
            # 配置每次读取、编译结果按规则缓存，兼顾实时生效和高频识别性能。
            customization_re = re.compile(r"%s" % self.customization)
            self._customization_re_cache[self.customization] = customization_re
        # 处理重复多次的情况，保留先后顺序（按添加自定义占位符的顺序）
        unique_customization: dict[str, int] = {}
        for item in customization_re.findall(title):
            if not isinstance(item, tuple):
                item = (item,)
            for i in range(len(item)):
                if item[i] and unique_customization.get(item[i]) is None:
                    unique_customization[item[i]] = i
        ordered_customization = list(dict(sorted(unique_customization.items(), key=lambda x: x[1])).keys())
        separator = self.custom_separator or "@"
        return separator.join(ordered_customization)
