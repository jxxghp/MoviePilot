from unittest import TestCase

from app.domain.title import is_media_title_like


class MediaTitleTest(TestCase):
    """验证媒体标题候选规则。"""

    def test_is_media_title_like_true(self):
        self.assertTrue(is_media_title_like("盗梦空间"))
        self.assertTrue(is_media_title_like("The Lord of the Rings"))
        self.assertTrue(is_media_title_like("庆余年 第2季"))
        self.assertTrue(is_media_title_like("The Office S01E01"))
        self.assertTrue(is_media_title_like("权力的游戏 Game of Thrones"))
        self.assertTrue(is_media_title_like("Spider-Man: No Way Home 2021"))

    def test_is_media_title_like_false(self):
        self.assertFalse(is_media_title_like(""))
        self.assertFalse(is_media_title_like("   "))
        self.assertFalse(is_media_title_like("a"))
        self.assertFalse(is_media_title_like("第2季"))
        self.assertFalse(is_media_title_like("S01E01"))
        self.assertFalse(is_media_title_like("#推荐电影"))
        self.assertFalse(is_media_title_like("请帮我推荐一部电影"))
        self.assertFalse(is_media_title_like("盗梦空间怎么样？"))
        self.assertFalse(is_media_title_like("我想看盗梦空间"))
        self.assertFalse(is_media_title_like("继续"))
