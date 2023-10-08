import regex as re

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton


class CustomizationMatcher(metaclass=Singleton):
    """
    识别自定义占位符
    """
    customization = None
    custom_separator = None

    def __init__(self):
        self.systemconfig = SystemConfigOper()
        self.customization = None
        self.custom_separator = None

    def match(self, title=None):
        """
        :param title: 资源标题或文件名
        :return: 匹配结果
        """
        if not title:
            return ""
        if not self.customization:
            # 自定义占位符
            customization = self.systemconfig.get(SystemConfigKey.Customization)
            if not customization:
                return ""
            if isinstance(customization, str):
                customization = customization.replace("\n", ";").replace("|", ";").strip(";").split(";")
            self.customization = "|".join([f"({item})" for item in customization])

        customization_re = re.compile(r"%s" % self.customization)
        # 处理重复多次的情况，保留先后顺序（按添加自定义占位符的顺序）
        unique_customization = {}
        for item in re.findall(customization_re, title):
            if not isinstance(item, tuple):
                item = (item,)
            for i in range(len(item)):
                if item[i] and unique_customization.get(item[i]) is None:
                    unique_customization[item[i]] = i
        unique_customization = list(dict(sorted(unique_customization.items(), key=lambda x: x[1])).keys())
        separator = self.custom_separator or "@"
        return separator.join(unique_customization)
