# 版权所有 (c) 2025 Nous Research；授权条款见随附的 MIT 许可证。
"""
供上下文、记忆与技能写入使用的提示注入和信息外传模式库。

每项由正则、模式标识和适用范围组成，范围按 all、context、strict 逐级累加。
all 检查通用攻击，context 增加角色劫持及控制通道提示，strict 仅用于可由用户修正的写入。
新增模式应锚定明确的控制通道词汇或攻击行为，避免把普通命令语气视为攻击；
关键词之间的填充必须受 _FILLER 长度限制。
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Tuple

# 扫描用于辅助检测，限制文本长度以约束最坏情况下的执行开销。
MAX_SCAN_CHARS = 65_536
# 限制攻击关键词之间的填充长度，避免无界单词重复导致严重回溯。
_FILLER = r"(?:\w+\s+){0,8}"
# 环境变量名称须以秘密值相关后缀结束，避免仅包含相同子串的普通变量被误判。
_SECRET_VAR = r"\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)S?\b"
# 配置修改模式要求出现明确的写入动作前缀。
_MODIFY = r"(update|modify|edit|write|change|append|add\s+to)\s+[^\n]{0,2048}"
# 每项依次包含正则、模式标识和范围；范围为 all、context 或 strict。
_PATTERNS: List[Tuple[str, str, str]] = [
    # 所有范围均检查的经典提示注入模式。
    (rf'ignore\s+{_FILLER}(previous|all|above|prior)\s+{_FILLER}instructions', "prompt_injection", "all"),
    (r'system\s+prompt\s+override', "sys_prompt_override", "all"),
    (rf'disregard\s+{_FILLER}(your|all|any)\s+{_FILLER}(instructions|rules|guidelines)', "disregard_rules", "all"),
    (rf'act\s+as\s+(if|though)\s+{_FILLER}you\s+{_FILLER}(have\s+no|don\'t\s+have)\s+{_FILLER}(restrictions|limits|rules)', "bypass_restrictions", "all"),
    (r'<!--[^>]{0,512}(?:ignore|override|system|secret|hidden)[^>]{0,512}-->', "html_comment_injection", "all"),
    (r'<\s*div\s+style\s*=\s*["\'][^>]{0,2048}display\s*:\s*none', "hidden_div", "all"),
    (
        r"translate\s+[^\n]{0,512}\s+into\s+\w+(?:[\s-]+\w+){0,2}\s+and\s+(execute|run|eval)\b",
        "translate_execute",
        "all",
    ),
    (rf'do\s+not\s+{_FILLER}tell\s+{_FILLER}the\s+user', "deception_hide", "all"),

    # 针对抓取网页或被污染上下文的角色与身份劫持模式。
    (rf'you\s+are\s+{_FILLER}now\s+(?:a|an|the)\s+', "role_hijack", "context"),
    (rf'pretend\s+{_FILLER}(you\s+are|to\s+be)\s+', "role_pretend", "context"),
    (rf'output\s+{_FILLER}(system|initial)\s+prompt', "leak_system_prompt", "context"),
    (rf'(respond|answer|reply)\s+without\s+{_FILLER}(restrictions|limitations|filters|safety)', "remove_filters", "context"),
    (rf'you\s+have\s+been\s+{_FILLER}(updated|upgraded|patched)\s+to', "fake_update", "context"),
    # 用成对动词限定通过规范覆盖身份的行为，避免误判给变量命名等正常说明。
    (r'\bname\s+yourself\s+\w+', "identity_override", "context"),

    # 控制通道模式锚定 C2 词汇，仅提供警告；合法分布式系统资料也可能描述节点注册。
    (r'register\s+(as\s+)?a?\s*node', "c2_node_registration", "context"),
    (r'(heartbeat|beacon|check[\s\-]?in)\s+(to|with)\s+', "c2_heartbeat", "context"),
    (r'pull\s+(down\s+)?(?:new\s+)?task(?:ing|s)?\b', "c2_task_pull", "context"),
    (r'connect\s+to\s+the\s+network\b', "c2_network_connect", "context"),
    # 使用 C2 专属动作词，避免普通强制语气造成误报。
    (r'you\s+must\s+(?:\w+\s+){0,3}(register|connect|report|beacon)\b', "forced_action", "context"),
    # 隐藏审计痕迹的指令在正常内容中罕见，可作为较明确的攻击信号。
    (r'only\s+use\s+one[\s\-]?liners?\b', "anti_forensic_oneliner", "context"),
    (rf'never\s+{_FILLER}(?:create|write)\s+{_FILLER}(?:script|file)\s+{_FILLER}disk', "anti_forensic_disk", "context"),
    # 清除智能体运行时环境变量可能用于绕过子会话约束。
    (r'unset\s+\w*(?:CLAUDE|CODEX|HERMES|AGENT|OPENAI|ANTHROPIC)\w*', "env_var_unset_agent", "context"),

    # 仅警告已知控制通道或红队框架名称；必须使用有辨识度的品牌词。
    # 普通英文词也可能是合法智能体名称，不能据此拒绝整份 AGENTS.md 或 SOUL.md。
    (r'\b(?:cobalt\s*strike|sliver|havoc|mythic|metasploit|brainworm)\b', "known_c2_framework", "context"),
    (r'\bc2\s+(?:server|channel|infrastructure|beacon)\b', "c2_explicit", "context"),
    (r'\bcommand\s+and\s+control\b', "c2_explicit_long", "context"),

    # 检查通过 curl、wget 或 cat 外传秘密值的行为。变量名称必须以秘密后缀结束。
    # 不把 API 作为秘密后缀，避免误判 TRILLIUM_ETAPI_URL 等普通地址变量；
    # OPENAI_API_KEY 等真实凭据已由 KEY 或 TOKEN 后缀覆盖。
    (rf'curl\s+[^\n]{{0,2048}}{_SECRET_VAR}', "exfil_curl", "all"),
    (rf'wget\s+[^\n]{{0,2048}}{_SECRET_VAR}', "exfil_wget", "all"),
    (r'cat\s+[^\n]{0,2048}(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)', "read_secrets", "all"),
    (r'(send|post|upload|transmit)\s+[^\n]{0,2048}\s+(to|at)\s+https?://', "send_to_url", "strict"),
    (rf'(include|output|print|share)\s+{_FILLER}(conversation|chat\s+history|previous\s+messages|full\s+context|entire\s+context)', "context_exfil", "strict"),

    # strict 范围额外检查持久化及 SSH 后门写入。
    (r'authorized_keys', "ssh_backdoor", "strict"),
    # 要求写入动作以避免仅提及路径的正常说明被阻止；同时覆盖重定向及脚本文件写入。
    # chmod、chown、sed、truncate、rm、touch、curl、wget 和 git 也可能改变目标目录。
    (r'(?:\b(?:echo|cat|cp|mv|dd|tee|install|printf|rsync|scp|ln|append|add|write'
     r'|sed|chmod|chown|truncate|rm|touch|curl|wget|git)\b|\bopen\s*\(|>>?)'
     r'[^\n]{0,512}(?:\$HOME/\.ssh|~/\.ssh)', "ssh_access", "strict"),
    (r'\$HOME/\.hermes/\.env|\~/\.hermes/\.env', "hermes_env", "strict"),
    (rf'{_MODIFY}(?:AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules)', "agent_config_mod", "strict"),
    (rf'{_MODIFY}\.hermes/(config\.yaml|SOUL\.md)', "hermes_config_mod", "strict"),

    # 硬编码秘密值检测跳过由至少两个大写下划线段构成的环境变量名称。
    # 例如 MYPLUGIN_APP_PASSWORD 可能只是凭据的来源名称，参见上游问题 #116221。
    # 此处局部区分大小写，仍检查小写下划线口令及没有下划线的大写凭据。
    (r'(?:api[_-]?key|token|secret|password)\s*[=:]\s*["\']'
     r'(?!(?-i:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)["\'])'
     r'[A-Za-z0-9+/=_-]{20,}', "hardcoded_secret", "strict"),
]

# 检测注入常用的不可见或双向 Unicode 控制字符，包括零宽字符、BOM、方向嵌入和隔离符。
INVISIBLE_CHARS = frozenset(
    "\u200b\u200c\u200d\u2060\u2062\u2063\u2064\ufeff"
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")

# 导入时按范围编译并逐级累加：all 包含于 context，context 包含于 strict。
_SCOPE_SETS = {"all": ("all", "context", "strict"), "context": ("context", "strict"), "strict": ("strict",)}


def _compile() -> dict[str, List[Tuple[re.Pattern[str], str]]]:
    """
    按适用范围累加编译模式，不扩大各模式本身的匹配范围。
    """
    compiled: dict[str, List[Tuple[re.Pattern[str], str]]] = {"all": [], "context": [], "strict": []}
    for pattern, pid, scope in _PATTERNS:
        if scope not in _SCOPE_SETS:
            raise ValueError(f"threat_patterns: unknown scope {scope!r} for pattern {pid!r}")
        for s in _SCOPE_SETS[scope]:
            compiled[s].append((re.compile(pattern, re.IGNORECASE), pid))
    return compiled


_COMPILED = _compile()


def scan_for_threats(content: str, scope: str = "context") -> List[str]:
    """
    扫描指定范围的威胁模式，并报告不可见字符。

    :param content: 待扫描文本，按固定上限截断以约束扫描开销
    :param scope: 检查范围，接受 all、context 或 strict
    :return: 命中的模式标识；不可见字符以 invisible_unicode_U+XXXX 形式标识
    :raises ValueError: 检查范围未知
    """
    if not content:
        return []
    if (patterns := _COMPILED.get(scope)) is None:
        raise ValueError(f"scan_for_threats: unknown scope {scope!r}")
    content = content[:MAX_SCAN_CHARS]
    # 先检查原始文本中的不可见字符，后续 NFKC 规范化可能移除这些字符。
    findings: List[str] = [f"invisible_unicode_U+{ord(ch):04X}" for ch in set(content) & INVISIBLE_CHARS]
    # NFKC 折叠全角及兼容字形，减少同形字绕过；它不处理跨文字体系的相似字符。
    # 跨文字体系混淆检测需要额外的 TR#39 数据库。
    normalised = unicodedata.normalize("NFKC", content)
    findings.extend(pid for compiled, pid in patterns if compiled.search(normalised))
    return findings


def first_threat_message(content: str, scope: str = "strict") -> Optional[str]:
    """
    返回首个威胁的公开错误说明，供命中即拒绝的写入入口使用。

    :param content: 待检查的写入文本
    :param scope: 累加模式的检查范围，默认采用严格检查
    :return: 首个威胁的错误说明；未命中时为 None
    """
    findings = scan_for_threats(content, scope=scope)
    if not findings:
        return None
    pid = findings[0]
    if pid.startswith("invisible_unicode_"):
        codepoint = pid.replace("invisible_unicode_", "")
        return f"Blocked: content contains invisible unicode character {codepoint} (possible injection)."
    return (f"Blocked: content matches threat pattern '{pid}'. "
            f"Content is injected into the system prompt and must not contain "
            f"injection or exfiltration payloads.")


__all__ = ["INVISIBLE_CHARS", "MAX_SCAN_CHARS", "scan_for_threats", "first_threat_message"]
