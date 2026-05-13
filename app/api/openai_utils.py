import hashlib
import time
import uuid
from typing import Any, Dict, List, Tuple


def _get_message_field(message: Any, field: str, default: Any = None) -> Any:
    if isinstance(message, dict):
        return message.get(field, default)
    return getattr(message, field, default)


def extract_text_and_images(content: Any) -> Tuple[str, List[str]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        return content.strip(), []

    text_parts: List[str] = []
    image_urls: List[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                normalized = item.strip()
                if normalized:
                    text_parts.append(normalized)
                continue
            if not isinstance(item, dict):
                continue
            item_type = (item.get("type") or "").lower()
            if item_type == "text":
                text = item.get("text")
                if text and str(text).strip():
                    text_parts.append(str(text).strip())
            elif item_type == "input_text":
                text = item.get("text")
                if text and str(text).strip():
                    text_parts.append(str(text).strip())
            elif item_type == "image_url":
                image_url = item.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                if url and str(url).strip():
                    image_urls.append(str(url).strip())
            elif item_type == "input_image":
                url = item.get("image_url")
                if url and str(url).strip():
                    image_urls.append(str(url).strip())
            elif item_type == "image":
                source = item.get("source") or {}
                if isinstance(source, dict) and source.get("type") == "base64":
                    data = source.get("data")
                    media_type = source.get("media_type") or "image/png"
                    if data and str(data).strip():
                        image_urls.append(
                            f"data:{media_type};base64,{str(data).strip()}"
                        )
    return "\n".join(text_parts).strip(), image_urls


def build_prompt(
    messages: List[Any], use_server_session: bool
) -> Tuple[str, List[str]]:
    system_texts: List[str] = []
    transcript: List[str] = []
    latest_user_text = ""
    latest_user_images: List[str] = []

    for message in messages:
        role = str(_get_message_field(message, "role", "user") or "user").lower()
        if role == "developer":
            role = "system"
        text, images = extract_text_and_images(_get_message_field(message, "content"))
        if role == "system":
            if text:
                system_texts.append(text)
            continue
        if role == "user":
            if text or images:
                latest_user_text = text
                latest_user_images = images
            if text:
                transcript.append(f"user: {text}")
            continue
        if text:
            transcript.append(f"{role}: {text}")

    if not latest_user_text and not latest_user_images:
        raise ValueError("No usable user message found in messages.")

    prompt_parts: List[str] = []
    if system_texts:
        prompt_parts.append("系统要求：\n" + "\n\n".join(system_texts))

    if not use_server_session and transcript:
        history = transcript[:-1] if transcript[-1].startswith("user: ") else transcript
        if history:
            prompt_parts.append("对话上下文：\n" + "\n".join(history[-10:]))

    if latest_user_text:
        prompt_parts.append("当前用户消息：\n" + latest_user_text)
    else:
        prompt_parts.append("当前用户消息：\n请结合图片内容回复。")

    return "\n\n".join(
        part for part in prompt_parts if part
    ).strip(), latest_user_images


def build_session_id(session_key: str, prefix: str) -> str:
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:32]}"


def build_completion_payload(content: str, model_id: str) -> Dict[str, Any]:
    created = int(time.time())
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": created,
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def build_responses_input(
    input_data: Any, instructions: str | None = None
) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    if instructions and str(instructions).strip():
        messages.append({"role": "system", "content": str(instructions).strip()})

    if isinstance(input_data, str):
        normalized = input_data.strip()
        if normalized:
            messages.append({"role": "user", "content": normalized})
        return messages

    if isinstance(input_data, list):
        for item in input_data:
            if not isinstance(item, dict):
                continue
            item_type = (item.get("type") or "").lower()
            if item_type == "message":
                role = item.get("role") or "user"
                content = item.get("content")
                messages.append({"role": role, "content": content})
            elif item.get("role") and "content" in item:
                messages.append(
                    {"role": item.get("role"), "content": item.get("content")}
                )
        return messages

    if (
        isinstance(input_data, dict)
        and input_data.get("role")
        and "content" in input_data
    ):
        messages.append(
            {"role": input_data.get("role"), "content": input_data.get("content")}
        )

    return messages


def build_anthropic_messages(system: Any, messages: List[Any]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    system_text, _ = extract_text_and_images(system)
    if system_text:
        normalized.append({"role": "system", "content": system_text})

    for message in messages:
        role = _get_message_field(message, "role", "user")
        content = _get_message_field(message, "content")
        normalized.append({"role": role, "content": content})
    return normalized
