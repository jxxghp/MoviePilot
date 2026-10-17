"""
媒体标题候选规则的有效和无效输入合同。
"""

from app.domain.title import is_media_title_like


def test_is_media_title_like_true():
    """
    中文与英文媒体标题、剧集和混合标题应被识别。
    """
    assert is_media_title_like('盗梦空间')
    assert is_media_title_like('The Lord of the Rings')
    assert is_media_title_like('庆余年 第2季')
    assert is_media_title_like('The Office S01E01')
    assert is_media_title_like('权力的游戏 Game of Thrones')
    assert is_media_title_like('Spider-Man: No Way Home 2021')


def test_is_media_title_like_false():
    """
    空白、单独剧集号、对话和检索指令不应被识别为标题。
    """
    assert not is_media_title_like('')
    assert not is_media_title_like('   ')
    assert not is_media_title_like('a')
    assert not is_media_title_like('第2季')
    assert not is_media_title_like('S01E01')
    assert not is_media_title_like('#推荐电影')
    assert not is_media_title_like('请帮我推荐一部电影')
    assert not is_media_title_like('盗梦空间怎么样？')
    assert not is_media_title_like('我想看盗梦空间')
    assert not is_media_title_like('继续')
