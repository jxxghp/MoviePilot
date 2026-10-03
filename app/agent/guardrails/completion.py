"""回复结束纠偏：识别尚未执行的尾部动作并限制续行次数。

保留短回复、尾部动作与语言上下文边界；中文尾部动作是同一规则的本地化。
"""

import re

_TRAILING_ACTION = re.compile(
    r"(?:\blet me now\b|\bi(?:['\u2019])?ll now\b|\bi will now\b"
    r"|\bnow i(?:['\u2019]ll| will)\b|\bnext[,:] i\b"
    r"|(?:接下来|现在)[，,]?我(?:会|将|要)|我(?:现在|接下来)(?:会|将|要)|让我(?:现在|接下来))"
    r"[^.!?\n。！？]{0,100}[.:。\u2026]?\s*$", re.IGNORECASE,
)
_ACK_FUTURE = re.compile(r"\b(i['’]ll|i will|let me|i can do that|i can help with that)\b")
_ACK_ACTIONS = ('look into', 'look at', 'inspect', 'scan', 'check', 'analyz', 'review', 'explore', 'read', 'open',
                'run', 'test', 'fix', 'debug', 'search', 'find', 'walkthrough', 'report back', 'summarize')
_WORKSPACE = ('directory', 'current dir', 'cwd', 'repo', 'repository', 'codebase', 'project', 'folder',
              'filesystem', 'file tree', 'files', 'path')
CONTINUATION_NUDGE = (
    '[System: Continue now. Execute the required tool calls and only send your final answer '
    'after completing the task.]'
)
FRAGMENT_NUDGE = (
    '[System: Your previous message ended the turn with a fragment that is not a usable answer. '
    'If the task is unfinished, continue it and then give the complete answer. If that fragment '
    'WAS your complete answer, send it again exactly as before.]'
)


def trailing_continue_intent(text: str) -> bool:
    """只检测四百字符内回复的最后一百六十字符，不把中途描述当结束意图。"""
    text = text.strip()
    return bool(text and len(text) <= 400 and _TRAILING_ACTION.search(text[-160:]))


def intermediate_ack(text: str, user_text: str, *, tool_results: int) -> bool:
    """Codex 专属默认：无工具回执的短未来动作回复还须有工作区上下文。"""
    text = text.strip().lower()
    if tool_results or not text or len(text) > 1200 or not _ACK_FUTURE.search(text):
        return False
    if not any(marker in text for marker in _ACK_ACTIONS):
        return False
    user_text = user_text.lower()
    return '/' in user_text or any(marker in user_text or marker in text for marker in _WORKSPACE)


def degenerate_final(text: str, user_text: str) -> bool:
    """只纠正异常标点开头或语言不符的极短碎片，正常数字、路径和中文短答不触发。"""
    text = text.strip()
    if not text or len(text) > 24 or text.endswith(('.', '!', '?', '。', '！', '？')):
        return False
    if text[0] in '?!,;:)]}' and len(text) > 1 and text[1].isalpha():
        return True
    if not any(ch.isalpha() for ch in text) or any(ch.isascii() and ch.isalnum() for ch in text):
        return False
    return not any(ch.isalpha() and not ch.isascii() for ch in user_text)
