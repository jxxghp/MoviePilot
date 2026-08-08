import inspect
from unittest import TestCase

from app.core.meta.config_source import get_meta_config, set_meta_config_provider
from app.core.meta.customization import CustomizationMatcher
from app.core.meta.releasegroup import ReleaseGroupsMatcher
from app.core.meta.words import WordsMatcher
from app.schemas.types import SystemConfigKey


class MetaConfigSourceTest(TestCase):
    """验证 core/meta 已与数据库解耦：无 provider 时纯算法可用，注入 provider 后生效。"""

    def tearDown(self):
        set_meta_config_provider(None)

    def test_meta_matcher_modules_no_longer_depend_on_db(self):
        """core/meta 的 matcher 模块不应再引用 app.db，实例也不应持有 DB 句柄。"""
        import app.core.meta.customization as customization_mod
        import app.core.meta.releasegroup as releasegroup_mod
        import app.core.meta.words as words_mod

        for mod in (words_mod, customization_mod, releasegroup_mod):
            self.assertNotIn("app.db", inspect.getsource(mod), f"{mod.__name__} 仍引用 app.db")

        self.assertFalse(hasattr(WordsMatcher(), "systemconfig"))
        self.assertFalse(hasattr(CustomizationMatcher(), "systemconfig"))
        self.assertFalse(hasattr(ReleaseGroupsMatcher(), "systemconfig"))

    def test_matchers_work_without_provider(self):
        """未注册 provider（无数据库）时，算法以“无自定义”语义工作。"""
        set_meta_config_provider(None)
        self.assertIsNone(get_meta_config(SystemConfigKey.CustomIdentifiers))
        # 无自定义识别词：标题原样返回、无应用记录
        title, applied = WordsMatcher().prepare("Some.Movie.2020.1080p")
        self.assertEqual(title, "Some.Movie.2020.1080p")
        self.assertEqual(applied, [])
        # 无自定义占位符：返回空
        self.assertEqual(CustomizationMatcher().match("[GROUP] Movie"), "")
        # 无自定义制作组：仍可识别内置组
        self.assertEqual(ReleaseGroupsMatcher().match("[FRDS] Movie"), "FRDS")

    def test_injected_provider_is_used(self):
        """注入 provider 后，三个 matcher 均读取注入的配置。"""
        config = {
            SystemConfigKey.CustomIdentifiers: ["屏蔽词"],
            SystemConfigKey.Customization: ["GROUP"],
            SystemConfigKey.CustomReleaseGroups: ["MyGroup"],
        }
        set_meta_config_provider(lambda key: config.get(key))
        # 自定义屏蔽词生效
        title, applied = WordsMatcher().prepare("电影 屏蔽词 2020")
        self.assertNotIn("屏蔽词", title)
        self.assertIn("屏蔽词", applied)
        # 自定义占位符生效
        self.assertEqual(CustomizationMatcher().match("[GROUP] Movie"), "GROUP")
        # 自定义制作组生效
        self.assertEqual(ReleaseGroupsMatcher().match("[MyGroup] Movie"), "MyGroup")
