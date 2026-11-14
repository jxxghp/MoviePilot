"""
为中间件提供系统消息的追加操作。
"""

from langchain_core.messages import ContentBlock, SystemMessage


def append_to_system_message(
        system_message: SystemMessage | None,
        text: str,
) -> SystemMessage:
    """
    将文本追加到系统消息。

    :param system_message: 已有系统消息；None 表示创建新的系统消息
    :param text: 待追加的文本，已有内容时自动用空行分隔
    :return: 包含新增文本块的新系统消息，不修改输入消息
    """
    new_content: list[ContentBlock] = list(system_message.content_blocks) if system_message else []  # noqa
    if new_content:
        text = f"\n\n{text}"
    new_content.append({"type": "text", "text": text})
    return SystemMessage(content_blocks=new_content)
