import regex as re

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton


class CustomizationMatcher(metaclass=Singleton):
    """
    识别自定义占位符
    """

    def __init__(self):
        self.systemconfig = SystemConfigOper()
        self.customization = None
        self.custom_separator = None
        self._customization_re_cache = {}

    @staticmethod
    def normalize_customization(customization):
        """
        规范化自定义占位符配置，兼容历史字符串与列表两种保存格式。
        """
        if isinstance(customization, str):
            customization = customization.replace("\n", ";").replace("|", ";").strip(";").split(";")
        if not customization:
            return []
        return list(filter(None, customization))

    @staticmethod
    def _normalize_customization(customization):
        """
        兼容旧调用，统一转到公开的自定义占位符规范化入口。
        """
        return CustomizationMatcher.normalize_customization(customization)

    def match(self, title=None):
        """
        :param title: 资源标题或文件名
        :return: 匹配结果
        """
        if not title:
            return ""
        # 自定义占位符需要跟随系统配置实时生效，避免单例缓存导致保存后仍沿用旧规则。
        customization = self.normalize_customization(
            self.systemconfig.get(SystemConfigKey.Customization)
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
        unique_customization = {}
        for item in customization_re.findall(title):
            if not isinstance(item, tuple):
                item = (item,)
            for i in range(len(item)):
                if item[i] and unique_customization.get(item[i]) is None:
                    unique_customization[item[i]] = i
        unique_customization = list(dict(sorted(unique_customization.items(), key=lambda x: x[1])).keys())
        separator = self.custom_separator or "@"
        return separator.join(unique_customization)
