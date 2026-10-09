from langchain_core.messages import ContentBlock, SystemMessage


def append_to_system_message(
        system_message: SystemMessage | None,
        text: str,
) -> SystemMessage:
    """将文本追加到系统消息。

    参数：
        system_message: 现有的系统消息或 None。
        text: 要添加到系统消息的文本。

    返回：
        追加了文本的新 SystemMessage。
    """
    new_content: list[ContentBlock] = list(system_message.content_blocks) if system_message else []  # noqa
    if new_content:
        text = f"\n\n{text}"
    new_content.append({"type": "text", "text": text})
    return SystemMessage(content_blocks=new_content)
