# 版权所有 (c) 2025 Nous Research；授权条款见随附的 MIT 许可证。
"""
对模型生成的文本修改执行分级模糊查找与替换。

STRATEGIES 按从严格到宽松的顺序匹配，容忍工具参数中的空白、缩进、
转义和 Unicode 字形差异，同时将替换位置映射回原始文本。
"""

import bisect
import re
from difflib import SequenceMatcher
from typing import Callable, Optional

Span = tuple[int, int]

IDENTICAL_STRINGS_ERROR = (
    "No edit was applied because old_string and new_string are identical. "
    "Provide the existing text to replace in old_string and the changed "
    "replacement text in new_string.")

UNICODE_MAP = {
    # 排版双引号
    "\u201c": '"', "\u201d": '"',
    # 排版单引号
    "\u2018": "'", "\u2019": "'",
    # 长短破折号
    "\u2014": "--", "\u2013": "-",
    # 省略号与不换行空格
    "\u2026": "...", "\u00a0": " ",
    # 数学排版负号
    "\u2212": "-",
    # Unicode 空格分隔符，避免仅因字形不同而进入相似度回退
    "\u2000": " ", "\u2001": " ", "\u2002": " ", "\u2003": " ",
    "\u2004": " ", "\u2005": " ", "\u2006": " ", "\u2007": " ",
    "\u2008": " ", "\u2009": " ", "\u200a": " ", "\u202f": " ",
    "\u205f": " ", "\u3000": " "}


def _unicode_normalize(text: str) -> str:
    """
    将排版用 Unicode 变体映射为对应的 ASCII 字符。
    """
    for char, repl in UNICODE_MAP.items():
        text = text.replace(char, repl)
    return text



def _calculate_line_positions(content_lines: list[str], start_line: int,
                              end_line: int, content_length: int) -> Span:
    """
    计算指定行切片覆盖的字符范围，结束位置不包含在范围内。
    """
    start_pos = sum(len(line) + 1 for line in content_lines[:start_line])
    end_pos = sum(len(line) + 1 for line in content_lines[:end_line]) - 1
    return start_pos, min(content_length, end_pos)


def _window_spans(content: str, content_lines: list[str], n: int,
                  accept: Callable[[int], bool]) -> list[Span]:
    """
    返回通过 accept 判定的各个连续 n 行窗口在原始文本中的字符范围。
    """
    return [
        _calculate_line_positions(content_lines, i, i + n, len(content))
        for i in range(len(content_lines) - n + 1) if accept(i)]


def _match_transformed_lines(content: str, pattern: str,
                             transform: Callable[[list[str]], list[str]]) -> list[Span]:
    """
    对每个行块应用指定变换后，比较待查片段与文件内容。
    """
    content_lines = content.split('\n')
    pattern_norm = transform(pattern.split('\n'))
    n = len(pattern_norm)
    return _window_spans(content, content_lines, n,
                         lambda i: transform(content_lines[i:i + n]) == pattern_norm)


def _strip_boundary(lines: list[str]) -> list[str]:
    """
    仅移除首行和末行的边界空白，保留中间行。
    """
    lines = list(lines)
    lines[0] = lines[0].strip()
    if len(lines) > 1:
        lines[-1] = lines[-1].strip()
    return lines


def _build_orig_to_norm_map(original: str) -> list[int]:
    """
    将原始字符位置映射到 Unicode 规范化后的字符位置。

    单个字符可能展开成多个字符；返回表长度为原文长度加 1，末项表示文本结束位置。
    """
    result: list[int] = []
    norm_pos = 0
    for char in original:
        result.append(norm_pos)
        repl = UNICODE_MAP.get(char)
        norm_pos += len(repl) if repl is not None else 1
    result.append(norm_pos)
    return result


def _invert_norm_map(orig_to_norm: list[int]) -> dict[int, int]:
    """
    查找映射到指定规范化位置的最早原始字符位置。
    """
    inverted: dict[int, int] = {}
    for orig_pos, norm_pos in enumerate(orig_to_norm[:-1]):
        inverted.setdefault(norm_pos, orig_pos)
    return inverted


def _norm_end_to_orig(orig_to_norm: list[int], orig_start: int, norm_end: int) -> int:
    """
    从原始起点向后查找，直到映射位置到达规范化结束位置。
    """
    orig_len = len(orig_to_norm) - 1
    orig_end = orig_start
    while orig_end < orig_len and orig_to_norm[orig_end] < norm_end:
        orig_end += 1
    return orig_end


def _map_positions_norm_to_orig(orig_to_norm: list[int], norm_matches: list[Span]) -> list[Span]:
    """
    将规范化文本中的匹配范围转换为原始文本范围。
    """
    norm_to_orig_start = _invert_norm_map(orig_to_norm)
    results: list[Span] = []
    for norm_start, norm_end in norm_matches:
        if norm_start in norm_to_orig_start:
            orig_start = norm_to_orig_start[norm_start]
            results.append((orig_start, _norm_end_to_orig(orig_to_norm, orig_start, norm_end)))
    return results


def _map_normalized_positions(original: str, normalized: str,
                              normalized_matches: list[Span]) -> list[Span]:
    """
    为连续空格或制表符折叠后的匹配结果尽力恢复原始字符范围。
    """
    # 记录每个原始字符位置在规范化文本中的对应位置。
    orig_to_norm = []
    orig_idx = norm_idx = 0
    while orig_idx < len(original) and norm_idx < len(normalized):
        if original[orig_idx] == normalized[norm_idx]:
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            norm_idx += 1
        elif original[orig_idx] in ' \t' and normalized[norm_idx] == ' ':
            # 整段空白消费完毕后才推进规范化位置，保持折叠前后的映射一致。
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            if orig_idx < len(original) and original[orig_idx] not in ' \t':
                norm_idx += 1
        else:
            # 原文多余空白或不应出现的规范化差异都映射到当前规范化位置。
            orig_to_norm.append(norm_idx)
            orig_idx += 1
    orig_to_norm.extend([len(normalized)] * (len(original) - orig_idx))

    norm_to_orig_start: dict[int, int] = {}
    norm_to_orig_end = {}
    for orig_pos, norm_pos in enumerate(orig_to_norm):
        norm_to_orig_start.setdefault(norm_pos, orig_pos)
        norm_to_orig_end[norm_pos] = orig_pos

    original_matches = []
    for norm_start, norm_end in normalized_matches:
        if norm_start in norm_to_orig_start:
            orig_start = norm_to_orig_start[norm_start]
        else:
            orig_start = min(i for i, n in enumerate(orig_to_norm) if n >= norm_start)
        if norm_end - 1 in norm_to_orig_end:
            orig_end = norm_to_orig_end[norm_end - 1] + 1
        else:
            orig_end = orig_start + (norm_end - norm_start)
        # 仅当规范化匹配以空格结尾时吸收末尾折叠空白；否则保留匹配后作为单词边界的首个空白。
        if norm_end < len(normalized) and normalized[norm_end - 1] == ' ':
            while orig_end < len(original) and original[orig_end] in ' \t':
                orig_end += 1
        original_matches.append((orig_start, min(orig_end, len(original))))
    return original_matches


# 每种策略接受原文和待查片段，返回原始文本中的起止字符范围。

def _strategy_exact(content: str, pattern: str) -> list[Span]:
    """
    使用不重叠的精确匹配，保持 str.replace 的替换语义。

    重叠范围会破坏 replace_all 的文本位置，因此不予返回。
    """
    return [m.span() for m in re.finditer(re.escape(pattern), content)]


def _strategy_line_trimmed(content: str, pattern: str) -> list[Span]:
    """
    去除每行首尾空白后比较。
    """
    return _match_transformed_lines(content, pattern, lambda ls: [line.strip() for line in ls])


def _strategy_whitespace_normalized(content: str, pattern: str) -> list[Span]:
    """
    把连续空格或制表符折叠为单个空格后比较。
    """
    def normalize(s: str) -> str:
        """
        折叠水平方向空白，同时保留行边界。
        """
        return re.sub(r'[ \t]+', ' ', s)

    content_normalized = normalize(content)
    matches_in_normalized = _strategy_exact(content_normalized, normalize(pattern))
    if not matches_in_normalized:
        return []
    return _map_normalized_positions(content, content_normalized, matches_in_normalized)


def _strategy_indentation_flexible(content: str, pattern: str) -> list[Span]:
    """
    忽略各行开头的缩进后比较。
    """
    return _match_transformed_lines(content, pattern, lambda ls: [line.lstrip() for line in ls])


def _strategy_escape_normalized(content: str, pattern: str) -> list[Span]:
    """
    把待查片段中的字面换行、制表和回车转义序列解释为控制字符后比较。
    """
    pattern_unescaped = pattern.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')
    if pattern_unescaped == pattern:
        return []
    return _strategy_exact(content, pattern_unescaped)


def _strategy_trimmed_boundary(content: str, pattern: str) -> list[Span]:
    """
    仅去除首尾行的边界空白后比较。
    """
    return _match_transformed_lines(content, pattern, _strip_boundary)


def _strategy_unicode_normalized(content: str, pattern: str) -> list[Span]:
    """
    两侧字符均规范化为 ASCII 后，尝试精确匹配及逐行去空白匹配。
    """
    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)
    if norm_content == content and norm_pattern == pattern:
        return []
    norm_matches = (_strategy_exact(norm_content, norm_pattern)
                    or _strategy_line_trimmed(norm_content, norm_pattern))
    if not norm_matches:
        return []
    return _map_positions_norm_to_orig(_build_orig_to_norm_map(content), norm_matches)


def _strategy_block_anchor(content: str, pattern: str) -> list[Span]:
    """
    以首尾行作为锚点，按中间行的相似度选择匹配块。
    """
    pattern_lines = _unicode_normalize(pattern).split('\n')
    if len(pattern_lines) < 2:
        return []
    first_line = pattern_lines[0].strip()
    last_line = pattern_lines[-1].strip()
    n = len(pattern_lines)

    # 在规范化行上匹配，但从原始行计算偏移，避免单字符展开造成位置偏移。
    norm_content_lines = _unicode_normalize(content).split('\n')
    potential_matches = {
        i for i in range(len(norm_content_lines) - n + 1)
        if norm_content_lines[i].strip() == first_line
        and norm_content_lines[i + n - 1].strip() == last_line}
    # 更宽松的阈值曾匹配到无关文本块，因此保留当前相似度下限。
    threshold = 0.50 if len(potential_matches) == 1 else 0.70
    pattern_middle = '\n'.join(pattern_lines[1:-1])

    def similar(i: int) -> bool:
        """
        按锚点之间的正文相似度阈值判断匹配。
        """
        if i not in potential_matches:
            return False
        if n <= 2:
            return True
        content_middle = '\n'.join(norm_content_lines[i + 1:i + n - 1])
        return SequenceMatcher(None, content_middle, pattern_middle).ratio() >= threshold

    return _window_spans(content, content.split('\n'), n, similar)


def _strategy_context_aware(content: str, pattern: str) -> list[Span]:
    """
    最后尝试有锚点的逐行相似匹配，要求每个非空行的相似度至少为 0.80。

    首尾锚点先缩小扫描范围，再逐行检查以减少偶然匹配。
    """
    pattern_lines = pattern.split('\n')
    content_lines = content.split('\n')
    n = len(pattern_lines)
    if n > len(content_lines):
        return []
    first_pat = pattern_lines[0].strip()
    last_pat = pattern_lines[-1].strip()

    def _sim(a: str, b: str) -> float:
        """
        比较去除首尾空白后的行，精确相同的行保持满分。
        """
        return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()

    def accept(i: int) -> bool:
        """
        要求首尾锚点及所有非空行均通过匹配检查。
        """
        block_lines = content_lines[i:i + n]
        if _sim(first_pat, block_lines[0].strip()) < 0.80:
            return False
        if _sim(last_pat, block_lines[-1].strip()) < 0.80:
            return False
        return all(
            not p_line.strip() or _sim(p_line.strip(), c_line.strip()) >= 0.80
            for p_line, c_line in zip(pattern_lines, block_lines))

    return _window_spans(content, content_lines, n, accept)


# 优先精确匹配，最后才尝试基于相似度的策略。
STRATEGIES: list[tuple[str, Callable[[str, str], list[Span]]]] = [
    ("exact", _strategy_exact),
    ("line_trimmed", _strategy_line_trimmed),
    ("whitespace_normalized", _strategy_whitespace_normalized),
    ("indentation_flexible", _strategy_indentation_flexible),
    ("escape_normalized", _strategy_escape_normalized),
    ("trimmed_boundary", _strategy_trimmed_boundary),
    ("unicode_normalized", _strategy_unicode_normalized),
    ("block_anchor", _strategy_block_anchor),
    ("context_aware", _strategy_context_aware)]

# 这些策略仅近似匹配旧片段，可用于唯一位置的替换，不能用于批量替换。
SIMILARITY_STRATEGIES = frozenset({"block_anchor", "context_aware"})



def is_already_applied(content: str, old_string: str, new_string: str) -> bool:
    """
    保守判断修改是否已经应用，避免重复提交同一次替换。

    :param content: 当前文件文本
    :param old_string: 原本需要替换的旧片段
    :param new_string: 期望写入的新片段
    :return: 新片段至少 8 个字符且已精确存在、旧片段已不存在时为 True
    """
    if not new_string or len(new_string.strip()) < 8 or new_string not in content:
        return False
    return old_string == new_string or old_string not in content


def _matched_regions(content: str, matches: list[Span]) -> str:
    """
    仅收集当前匹配策略选中的原始文本区域。
    """
    return "".join(content[start:end] for start, end in matches)


def _format_match_locations(content: str, matches: list[Span], cap: int = 5) -> str:
    """
    将最多 cap 个匹配位置渲染为带行号的短片段。
    """
    rows = []
    for start, _end in matches[:cap]:
        line_no = content.count("\n", 0, start) + 1
        line_start = content.rfind("\n", 0, start) + 1
        line_end = content.find("\n", line_start)
        if line_end == -1:
            line_end = len(content)
        snippet = content[line_start:line_end].strip()
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        rows.append(f"  L{line_no}: {snippet}")
    extra = len(matches) - cap
    if extra > 0:
        rows.append(f"  ... and {extra} more")
    return "\n".join(rows)


def fuzzy_find_and_replace(content: str, old_string: str, new_string: str,
                           replace_all: bool = False) -> tuple[str, int, Optional[str], Optional[str]]:
    """
    依次尝试匹配策略并替换选中的文本区域。

    :param content: 待修改的原始文件文本
    :param old_string: 用于定位的非空旧片段，不接受纯空白
    :param new_string: 替换内容，空字符串表示删除
    :param replace_all: 是否替换全部匹配；近似相似策略不允许批量替换
    :return: 新文本、匹配数量、策略名称及错误说明；失败时保留原文并返回零匹配
    """
    if not old_string:
        # 给出可执行的恢复建议，避免模型在空旧片段错误后不断重复同一调用；参见 cline/cline#13970。
        return content, 0, None, (
            "old_string is empty — nothing to match. Set old_string to the exact "
            "existing text the replacement should replace (read the file first if "
            "unsure). To create a new file or fully rewrite one, use write_file "
            "instead. Do not re-send this call unchanged.")
    if not old_string.strip():
        # 纯空白锚点容易命中大量区域或产生歧义，不能可靠定位编辑目标。
        return content, 0, None, (
            "old_string is only whitespace — provide non-blank text to match. Set it "
            "to the exact existing text the replacement should replace (read the file "
            "first if unsure). Do not re-send this call unchanged.")
    if old_string == new_string:
        return content, 0, None, IDENTICAL_STRINGS_ERROR

    for strategy_name, strategy_fn in STRATEGIES:
        matches = strategy_fn(content, old_string)
        if not matches:
            continue

        if len(matches) > 1 and not replace_all:
            locations = _format_match_locations(content, matches)
            return content, 0, None, (
                f"Found {len(matches)} matches for old_string. "
                f"Provide more context to make it unique, or use replace_all=True. "
                f"Matches:\n{locations}")
        if replace_all and len(matches) > 1 and strategy_name in SIMILARITY_STRATEGIES:
            return content, 0, None, (
                f"Found {len(matches)} approximate matches via the "
                f"'{strategy_name}' strategy; replace_all only applies to exact "
                f"matches. Provide the precise text (whitespace included) so an "
                f"exact/line-trimmed match can be made.")

        # 非精确匹配经过规范化，新片段可能携带原文件没有的序列化转义偏差。
        if strategy_name != "exact":
            drift_err = _detect_escape_drift(content, matches, old_string, new_string)
            if drift_err:
                return content, 0, None, drift_err

        effective_new = _maybe_unescape_new_string(new_string, content, matches)
        if strategy_name == "unicode_normalized":
            effective_new = _preserve_unicode_in_replacement(content, matches, old_string, effective_new)
        new_content = _apply_replacements(
            content, matches, effective_new,
            old_string=old_string if strategy_name != "exact" else None)
        return new_content, len(matches), strategy_name, None

    return content, 0, None, "Could not find a match for old_string in the file"



def _detect_newline_literal_drift(content: str, matches: list[Span],
                                  old_string: str, new_string: str) -> Optional[str]:
    """
    检测工具参数被额外 JSON 转义，导致字面换行序列代替文件中的真实换行。

    逐个匹配区域比较字面转义数量，避免 replace_all 把多个区域计数叠加。
    只有旧片段比对应文件区域多出字面换行序列，且新片段也包含该序列时才报告错误。
    只在新片段中出现的转义无法与有意新增字面文本区分；回车由后续解码逻辑处理。

    :return: 可确认转义漂移时的错误说明，否则为 None
    """
    if "\\n" not in new_string:
        return None
    old_literals = old_string.count("\\n")
    for start, end in matches:
        region = content[start:end]
        if "\n" in region and old_literals > region.count("\\n"):
            return (
                "Escape-drift detected: old_string contains more literal "
                "'\\\\n' sequences than the matched region of the file, which "
                "has real line breaks there instead. This is almost always a "
                "tool-call serialization artifact where a line break got escaped "
                "one extra time; new_string would write it as backslash + n. "
                "Re-read the file with read_file and pass old_string/new_string "
                "with actual line breaks, keeping only the '\\\\n' sequences "
                "that appear literally in the file.")
    return None


def _detect_escape_drift(content: str, matches: list[Span],
                         old_string: str, new_string: str) -> Optional[str]:
    """
    检查新片段是否携带额外引号转义、加倍反斜杠或代替真实换行的字面序列。

    :return: 发现工具参数转义漂移时的错误说明，否则为 None
    """
    has_quote_suspects = "\\'" in new_string or '\\"' in new_string
    if not has_quote_suspects and "\\" not in old_string:
        return None

    matched_regions = _matched_regions(content, matches)
    if has_quote_suspects:
        for suspect in ("\\'", '\\"'):
            if suspect in new_string and suspect in old_string and suspect not in matched_regions:
                plain = suspect[1]
                return (
                    f"Escape-drift detected: old_string and new_string contain "
                    f"the literal sequence {suspect!r} but the matched region of "
                    f"the file does not. This is almost always a tool-call "
                    f"serialization artifact where an apostrophe or quote got "
                    f"prefixed with a spurious backslash. Re-read the file with "
                    f"read_file and pass old_string/new_string without "
                    f"backslash-escaping {plain!r} characters.")
    newline_drift = _detect_newline_literal_drift(content, matches, old_string, new_string)
    if newline_drift:
        return newline_drift
    return _detect_backslash_doubling(matched_regions, old_string, new_string)


def _backslash_runs(s: str) -> list[int]:
    """
    按出现顺序返回各段连续反斜杠的长度。
    """
    return [len(run) for run in re.findall(r"\\+", s)]


def _detect_backslash_doubling(matched_regions: str, old_string: str,
                               new_string: str) -> Optional[str]:
    """
    检测旧片段中每段反斜杠是否均为文件对应长度的两倍。

    要求连续段数量一致，至少有足够明确的加倍信号，且新片段尚未符合文件中的长度。
    """
    old_runs = _backslash_runs(old_string)
    file_runs = _backslash_runs(matched_regions)
    if (not old_runs or not file_runs or len(old_runs) != len(file_runs)
            or old_runs == file_runs
            or any(o != f * 2 for o, f in zip(old_runs, file_runs))
            or not (any(f >= 2 for f in file_runs) or len(file_runs) >= 2)
            or _backslash_runs(new_string) == file_runs):
        return None
    return (
        "Escape-drift detected: every backslash run in old_string is exactly "
        "twice as long as in the matched region of the file (e.g. the file "
        "has `\\\\` where old_string has `\\\\\\\\`). The tool-call arguments "
        "were JSON-escaped one extra time; applying new_string verbatim would "
        "double every backslash in the file. Re-read the file with read_file "
        "and resend old_string/new_string with the backslash counts exactly "
        "as they appear in the file.")


def _maybe_unescape_new_string(new_string: str, content: str, matches: list[Span]) -> str:
    """
    仅在匹配区域已包含真实控制字符时，将新片段中的对应制表或回车序列还原。

    不处理字面换行序列，以免破坏源码中有意保留的转义文本。
    """
    if "\\t" not in new_string and "\\r" not in new_string:
        return new_string
    matched_regions = _matched_regions(content, matches)
    for literal, control in (("\\t", "\t"), ("\\r", "\r")):
        if literal in new_string and control in matched_regions:
            new_string = new_string.replace(literal, control)
    return new_string



def _leading_whitespace(line: str) -> str:
    """
    取得替换时需要保留的行首缩进。
    """
    return line[:len(line) - len(line.lstrip(" \t"))]


def _first_meaningful_line(text: str) -> Optional[str]:
    """
    选择第一个非空行作为缩进基准。
    """
    return next((line for line in text.split("\n") if line.strip()), None)


def _reindent_replacement(file_region: str, old_string: str, new_string: str) -> str:
    """
    非精确匹配后，将新片段的基础缩进对齐到文件中的实际缩进。

    保留相对嵌套层级；浅于旧片段基准的行也对齐到文件基准。
    """
    if not new_string:
        return new_string
    old_first = _first_meaningful_line(old_string)
    file_first = _first_meaningful_line(file_region)
    if old_first is None or file_first is None:
        return new_string
    old_indent = _leading_whitespace(old_first)
    file_indent = _leading_whitespace(file_first)
    if old_indent == file_indent:
        return new_string

    out_lines: list[str] = []
    for line in new_string.split("\n"):
        if not line.strip():
            out_lines.append(line)
        elif _leading_whitespace(line).startswith(old_indent):
            out_lines.append(file_indent + line[len(old_indent):])
        else:
            out_lines.append(file_indent + line.lstrip(" \t"))
    return "\n".join(out_lines)


def _preserve_unicode_in_replacement(content: str, matches: list[Span],
                                     old_string: str, new_string: str) -> str:
    """
    仅把旧片段到新片段的真实编辑应用到文件，保留原文未修改的 Unicode 排版字符。
    """
    file_region = _matched_regions(content, matches)
    norm_old = _unicode_normalize(old_string)
    if norm_old != _unicode_normalize(file_region):
        # 规范化结果未能匹配时回退，避免错误应用该策略。
        return new_string

    file_orig_to_norm = _build_orig_to_norm_map(file_region)

    result_parts: list[str] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, norm_old, new_string).get_opcodes():
        if tag == "equal":
            # 定位规范化位置对应的原始字符，包括处在单字符展开序列内部的位置。
            orig_start = bisect.bisect_right(file_orig_to_norm, i1) - 1
            orig_end = _norm_end_to_orig(file_orig_to_norm, orig_start, i2)
            result_parts.append(file_region[orig_start:orig_end])
        elif tag != "delete":
            result_parts.append(new_string[j1:j2])
    return "".join(result_parts)


def _apply_replacements(content: str, matches: list[Span],
                        new_string: str, old_string: Optional[str] = None) -> str:
    """
    从后向前应用替换以保持偏移有效，非精确匹配时按每个区域重排缩进。
    """
    result = content
    for start, end in sorted(matches, key=lambda x: x[0], reverse=True):
        adjusted = new_string
        if old_string is not None:
            adjusted = _reindent_replacement(content[start:end], old_string, new_string)
        result = result[:start] + adjusted + result[end:]
    return result



def _visualize_whitespace(line: str) -> str:
    """
    可视化行首空白：箭头表示制表符，中点表示空格。
    """
    stripped = line.lstrip(" \t")
    prefix = line[:len(line) - len(stripped)]
    return prefix.replace("\t", "→").replace(" ", "·") + stripped


def find_closest_lines(old_string: str, content: str, context_lines: int = 2, max_results: int = 3) -> str:
    """
    查找最接近旧片段锚点行的文件行，返回带行号的片段；没有候选时返回空字符串。
    """
    if not old_string or not content:
        return ""
    old_lines = old_string.splitlines()
    content_lines = content.splitlines()
    if not old_lines or not content_lines:
        return ""

    anchor = old_lines[0].strip() or next((line.strip() for line in old_lines if line.strip()), "")
    if not anchor:
        return ""

    scored = sorted(((SequenceMatcher(None, anchor, line.strip()).ratio(), i)
                     for i, line in enumerate(content_lines) if line.strip()), key=lambda x: -x[0])
    top = [s for s in scored if s[0] > 0.3][:max_results]
    if not top:
        return ""

    parts = []
    seen_ranges = set()
    for _, line_idx in top:
        start = max(0, line_idx - context_lines)
        end = min(len(content_lines), line_idx + len(old_lines) + context_lines)
        if (start, end) in seen_ranges:
            continue
        seen_ranges.add((start, end))
        parts.append("\n".join(
            f"{start + j + 1:4d}| {content_lines[start + j]}" for j in range(end - start)))
    result = "\n---\n".join(parts)

    # 去除空白后已相同的候选行需可视化缩进，使模型能按文件实际缩进修正输入。
    best_line = content_lines[top[0][1]]
    if best_line.strip() == anchor and best_line != old_lines[0]:
        result += (
            "\n\nWhitespace difference detected (→ = tab, · = space):\n"
            f"  file has: {_visualize_whitespace(best_line)}\n"
            f"  you sent: {_visualize_whitespace(old_lines[0])}\n"
            "Use the exact whitespace shown in 'file has'.")
    return result


def format_no_match_hint(error: Optional[str], match_count: int,
                         old_string: str, content: str) -> str:
    """
    仅为普通未匹配错误提供相似行提示。

    歧义、转义漂移和内容相同等错误虽然也返回零匹配，但相似行提示会误导重试，因此不追加。
    """
    if match_count != 0 or not error or not error.startswith("Could not find"):
        return ""
    hint = find_closest_lines(old_string, content)
    return "\n\nDid you mean one of these sections?\n" + hint if hint else ""
