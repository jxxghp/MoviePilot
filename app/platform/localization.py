import json
import re
from contextvars import ContextVar, Token
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional


class LocaleHelper:
    """
    后端多语言文本辅助器。

    该类只为需要返回给前端展示的文本生成并行多语言字段，旧有中文字段仍由调用方保留。
    """

    DEFAULT_LOCALE = "zh-CN"
    SUPPORTED_LOCALES = ("zh-CN", "zh-TW", "en-US")
    HEADER_NAMES = ("x-moviepilot-locale", "x-locale")
    _PATTERN_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
    _CURRENT_LOCALE: ContextVar[str] = ContextVar("moviepilot_locale", default=DEFAULT_LOCALE)
    _LOCALES_DIR = Path(__file__).resolve().parents[1] / "locales"
    _LOCALE_ALIASES = {
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "zh-hans": "zh-CN",
        "zh-hans-cn": "zh-CN",
        "zh-tw": "zh-TW",
        "zh-hant": "zh-TW",
        "zh-hant-tw": "zh-TW",
        "en": "en-US",
        "en-us": "en-US",
    }

    @classmethod
    def normalize_locale(cls, locale: Optional[str]) -> str:
        """
        规范化语言标识，无法识别时返回默认简体中文。

        :param locale: 原始语言标识，如 zh-CN、zh_CN、en-US
        :return: 项目支持的语言标识
        """
        return cls._match_locale(locale) or cls.DEFAULT_LOCALE

    @classmethod
    def get_locale_from_request(cls, request: Any) -> str:
        """
        从请求参数或请求头解析前端期望语言。

        :param request: FastAPI Request 或带 headers 属性的兼容对象
        :return: 项目支持的语言标识
        """
        query_params = getattr(request, "query_params", {}) or {}
        query_locale = query_params.get("locale") if hasattr(query_params, "get") else None
        if query_locale:
            return cls.normalize_locale(query_locale)

        headers = getattr(request, "headers", {}) or {}
        for header_name in cls.HEADER_NAMES:
            value = headers.get(header_name)
            if value:
                return cls.normalize_locale(value)

        accept_language = headers.get("accept-language")
        if not accept_language:
            return cls.DEFAULT_LOCALE

        choices = []
        for index, item in enumerate(accept_language.split(",")):
            parts = [part.strip() for part in item.split(";") if part.strip()]
            if not parts:
                continue
            quality = 1.0
            for part in parts[1:]:
                if part.startswith("q="):
                    try:
                        quality = float(part[2:])
                    except ValueError:
                        quality = 0.0
            choices.append((-quality, index, parts[0]))

        for _, _, candidate in sorted(choices):
            locale = cls._match_locale(candidate)
            if locale:
                return locale
        return cls.DEFAULT_LOCALE

    @classmethod
    def get_current_locale(cls) -> str:
        """
        获取当前请求上下文中的语言标识。

        :return: 项目支持的语言标识
        """
        return cls._CURRENT_LOCALE.get()

    @classmethod
    def set_current_locale(cls, locale: Optional[str]) -> Token[str]:
        """
        设置当前请求上下文中的语言标识。

        :param locale: 原始语言标识
        :return: 用于恢复上下文的令牌
        """
        return cls._CURRENT_LOCALE.set(cls.normalize_locale(locale))

    @classmethod
    def reset_current_locale(cls, token: Token[str]) -> None:
        """
        恢复当前请求上下文中的语言标识。

        :param token: set_current_locale 返回的上下文令牌
        """
        cls._CURRENT_LOCALE.reset(token)

    @classmethod
    def translate(
            cls,
            key: str,
            locale: Optional[str] = None,
            default: Optional[str] = None,
            **kwargs: Any,
    ) -> str:
        """
        根据翻译键获取多语言文本。

        :param key: 点分隔翻译键
        :param locale: 目标语言，未传入或无法识别时使用默认语言
        :param default: 翻译缺失时返回的默认文本
        :param kwargs: 字符串格式化参数
        :return: 翻译后的文本
        """
        normalized_locale = cls.normalize_locale(locale) if locale else cls.get_current_locale()
        template = cls._lookup(cls._load_catalog(normalized_locale), key)
        if template is None and normalized_locale != cls.DEFAULT_LOCALE:
            template = cls._lookup(cls._load_catalog(cls.DEFAULT_LOCALE), key)
        if template is None:
            template = default or key
        return cls._format(template, kwargs)

    @classmethod
    def translate_text(cls, text: Optional[str], locale: Optional[str] = None) -> str:
        """
        翻译存量接口返回的中文文本。

        :param text: 原始中文文本
        :param locale: 目标语言，未传入或无法识别时使用默认语言
        :return: 翻译后的文本，缺失翻译时返回原文
        """
        if not text:
            return ""
        normalized_locale = cls.normalize_locale(locale) if locale else cls.get_current_locale()
        translated = cls._lookup_message(cls._load_catalog(normalized_locale), text)
        if translated is None and cls._contains_chinese(text):
            translated = cls._lookup_pattern(normalized_locale, text)
        if translated is None and normalized_locale != cls.DEFAULT_LOCALE:
            translated = cls._lookup_message(cls._load_catalog(cls.DEFAULT_LOCALE), text)
        if (
                translated is None
                and normalized_locale != cls.DEFAULT_LOCALE
                and cls._contains_chinese(text)
        ):
            translated = cls._lookup_pattern(cls.DEFAULT_LOCALE, text)
        return translated or text

    @classmethod
    def _match_locale(cls, locale: Optional[str]) -> Optional[str]:
        """
        将原始语言标识匹配为项目支持的语言。
        """
        if not locale:
            return None
        normalized = locale.strip().replace("_", "-").lower()
        if not normalized:
            return None
        return cls._LOCALE_ALIASES.get(normalized)

    @staticmethod
    @lru_cache(maxsize=16)
    def _load_catalog(locale: str) -> dict[str, Any]:
        """
        加载指定语言的翻译表。
        """
        catalog_path = LocaleHelper._LOCALES_DIR / f"{locale}.json"
        try:
            with catalog_path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _lookup(catalog: dict[str, Any], key: str) -> Optional[str]:
        """
        按点分隔键从结构化翻译表中查找文本。
        """
        current: Any = catalog
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current if isinstance(current, str) else None

    @staticmethod
    def _lookup_message(catalog: dict[str, Any], text: str) -> Optional[str]:
        """
        从精确消息表中查找存量中文文本。
        """
        messages = catalog.get("messages")
        if not isinstance(messages, dict):
            return None
        translated = messages.get(text)
        return translated if isinstance(translated, str) else None

    @classmethod
    def _lookup_pattern(cls, locale: str, text: str) -> Optional[str]:
        """
        使用动态模板匹配存量中文文本。
        """
        for pattern, target in cls._load_pattern_matchers(locale):
            matched = pattern.fullmatch(text)
            if matched:
                return cls._format(
                    target,
                    cls._build_pattern_values(locale, matched.groupdict()),
                )
        return None

    @classmethod
    def _build_pattern_values(cls, locale: str, values: dict[str, str]) -> dict[str, str]:
        """
        为动态模板补充可选的占位值翻译。
        """
        pattern_values = dict(values)
        catalog = cls._load_catalog(locale)
        default_catalog = (
            cls._load_catalog(cls.DEFAULT_LOCALE)
            if locale != cls.DEFAULT_LOCALE
            else catalog
        )
        for name, value in values.items():
            translated = cls._lookup_message(catalog, value)
            if translated is None and locale != cls.DEFAULT_LOCALE:
                translated = cls._lookup_message(default_catalog, value)
            pattern_values[f"{name}_i18n"] = translated or value
        return pattern_values

    @staticmethod
    @lru_cache(maxsize=16)
    def _load_pattern_matchers(locale: str) -> list[tuple[re.Pattern[str], str]]:
        """
        加载并缓存指定语言的动态文本匹配器。
        """
        catalog = LocaleHelper._load_catalog(locale)
        patterns = catalog.get("message_patterns")
        if not isinstance(patterns, list):
            return []
        matchers = []
        for item in patterns:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            target = item.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            pattern = LocaleHelper._compile_pattern(source)
            if pattern is None:
                continue
            matchers.append((pattern, target))
        return matchers

    @classmethod
    def _compile_pattern(cls, source: str) -> Optional[re.Pattern[str]]:
        """
        将带命名占位符的中文模板编译为正则。
        """
        field_names = cls._PATTERN_FIELD.findall(source)
        if not field_names:
            return None

        pattern = cls._PATTERN_FIELD.sub(
            lambda match: f"(?P<{match.group(1)}>.+?)",
            re.escape(source).replace(r"\{", "{").replace(r"\}", "}"),
        )
        return re.compile(pattern)

    @staticmethod
    def _contains_chinese(text: str) -> bool:
        """
        判断文本是否包含中文字符。
        """
        return any("\u4e00" <= char <= "\u9fff" for char in text)

    @staticmethod
    def _format(template: str, kwargs: dict[str, Any]) -> str:
        """
        格式化翻译模板，参数缺失时保留模板原文。
        """
        if not kwargs:
            return template
        try:
            return template.format(**kwargs)
        except (KeyError, AttributeError, IndexError):
            return template
