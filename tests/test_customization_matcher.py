from unittest import TestCase

from app.core.meta.config_source import set_meta_config_provider
from app.core.meta.customization import CustomizationMatcher


class CustomizationMatcherTest(TestCase):
    def tearDown(self):
        # 还原注入，避免污染其它用例
        set_meta_config_provider(None)

    def test_match_uses_latest_customization_setting(self):
        """自定义占位符修改后，下一次识别应直接使用新配置。"""
        matcher = CustomizationMatcher()
        values = [["GROUP"], ["TEAM"]]
        # 通过注入的 config provider 提供配置，无需数据库
        set_meta_config_provider(lambda _: values[0])
        self.assertEqual(matcher.match("[GROUP][TEAM] Movie"), "GROUP")
        values[0] = ["TEAM"]
        self.assertEqual(matcher.match("[GROUP][TEAM] Movie"), "TEAM")
