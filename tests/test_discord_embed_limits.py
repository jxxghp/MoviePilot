"""
验证 Discord 嵌入消息构建时遵守平台长度上限。
"""

from app.modules.discord.discord import Discord
from app.schemas.types import MessageType


def _embed_total(embed) -> int:
    """
    统计嵌入消息中计入 6000 字符上限的全部文本长度。
    """
    total = len(embed.title or "") + len(embed.description or "")
    for field in embed.fields:
        total += len(field.name) + len(field.value)
    return total


def test_long_title_and_description_are_truncated():
    """
    超长标题和描述应被截断到 256 / 4096 字符并以省略号结尾。
    """
    embed = Discord._build_embed(
        title="题" * 1000, text="文" * 9000, image=None, link=None
    )

    assert len(embed.title) == 256
    assert embed.title.endswith("…")
    assert len(embed.description) == 4096
    assert embed.description.endswith("…")


def test_long_field_name_and_value_are_truncated():
    """
    超长字段名和值分别被截断到 256 和 1024 字符。
    """
    embed = Discord._build_embed(
        title="标题",
        text="名" * 3000 + "：" + "值" * 3000,
        image=None,
        link=None,
        mtype=MessageType.Download,
    )

    assert len(embed.fields) == 1
    assert len(embed.fields[0].name) == 256
    assert embed.fields[0].name.endswith("…")
    assert len(embed.fields[0].value) == 1024
    assert embed.fields[0].value.endswith("…")


def test_field_count_and_total_length_are_limited():
    """
    字段最多 25 个，整条嵌入合计不超过 6000 字符。
    """
    text = "\n".join(f"字段{i}：{'值' * 900}" for i in range(40))
    embed = Discord._build_embed(
        title="标题",
        text="描述" * 2000 + "\n" + text,
        image=None,
        link=None,
        mtype=MessageType.Download,
    )

    assert len(embed.fields) <= 25
    assert _embed_total(embed) <= 6000


def test_short_content_is_unchanged():
    """
    未超限的内容保持原样。
    """
    embed = Discord._build_embed(
        title="标题",
        text="名称：值\n说明",
        image=None,
        link=None,
        mtype=MessageType.Download,
    )

    assert embed.title == "标题"
    assert embed.description == "说明"
    assert [(f.name, f.value) for f in embed.fields] == [("名称", "值")]


def test_text_only_notification_without_title():
    """
    仅有正文、标题为空（None）的通知也应能构建嵌入消息，而不是抛出异常。
    """
    embed = Discord._build_embed(title=None, text="仅正文通知", image=None, link=None)

    assert not embed.title
    assert embed.description == "仅正文通知"
