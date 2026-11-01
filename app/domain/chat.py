"""
会话展示快照中选择结果的合并规则，不负责交互执行或持久化。
"""

from copy import deepcopy
from typing import Any


def _choice_selections(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    优先采用完整选择回执，旧记录只有已选卡片时从卡片恢复。
    """
    selections: dict[str, dict[str, Any]] = {}
    for message in messages:
        selected = message.get("choice_selection")
        if isinstance(selected, dict) and selected.get("choice_id"):
            selections.setdefault(str(selected["choice_id"]), selected)
    for message in messages:
        for choice in message.get("choices") or []:
            if isinstance(choice, dict) and choice.get("status") == "selected" and choice.get("id"):
                selections.setdefault(str(choice["id"]), {
                    "choice_id": str(choice["id"]),
                    "prompt": choice.get("prompt") or "",
                    "buttons": choice.get("buttons") or [],
                    "button_rows": choice.get("button_rows") or [],
                    **{key: choice.get(key) for key in (
                        "title", "selected_label", "selected_value", "selected_description",
                    )},
                })
    return selections


def merge_choice_state(
    previous: list[dict[str, Any]], messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    保留首次已选结果，旧客户端的 pending 快照不能降级已选卡片。

    :param previous: 当前持久化的权威快照
    :param messages: 本次准备保存的完整快照，不原地修改
    :return: 应用已选结果并按稳定 ID 合并选择回显后的快照
    """
    selections = _choice_selections(previous)
    for choice_id, selection in _choice_selections(messages).items():
        selections.setdefault(choice_id, selection)
    result: list[dict[str, Any]] = []
    receipts: dict[str, int] = {}
    received_choices: set[str] = set()
    for original in messages:
        message = deepcopy(original)
        selected = message.get("choice_selection")
        if isinstance(selected, dict) and selected.get("choice_id"):
            choice_id = str(selected["choice_id"])
            received_choices.add(choice_id)
            message["choice_selection"] = deepcopy(selections[choice_id])
        for choice in message.get("choices") or []:
            if not isinstance(choice, dict) or str(choice.get("id") or "") not in selections:
                continue
            selection = selections[str(choice["id"])]
            choice.update({"status": "selected", **{
                key: selection.get(key) for key in ("selected_label", "selected_value", "selected_description")
            }})
        key = str(message.get("id") or "") if selected else ""
        if key and key in receipts:
            result[receipts[key]] = message
        else:
            if key:
                receipts[key] = len(result)
            result.append(message)
    for choice_id, selection in selections.items():
        if choice_id not in received_choices:
            result.append({
                "id": f"choice-selection-{choice_id}", "role": "user", "content": "",
                "choice_selection": deepcopy(selection),
            })
    return result
