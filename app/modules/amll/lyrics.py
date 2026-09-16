"""把 AMLL 的主唱 TTML 时轴转换为宿主歌词候选。"""

import re
from dataclasses import dataclass, replace
from io import StringIO
from typing import Optional

from lxml import etree
from ruamel.yaml import YAML

from app.domain.context import MusicLyrics

_TTML = "{http://www.w3.org/ns/ttml}"
_METADATA = "{http://www.w3.org/ns/ttml#metadata}"
_STYLING = "{http://www.w3.org/ns/ttml#styling}"
_XML = "{http://www.w3.org/XML/1998/namespace}"
_MAX_BYTES = 1024 * 1024
_MAX_DEPTH = 20
_MAX_NODES = 10000
_TIMESTAMP = re.compile(r"(?:[0-9]+:){0,2}[0-9]+(?:\.[0-9]+)?\Z")
_AUXILIARY_ROLES = frozenset({"x-translation", "x-roman", "x-bg"})
_RUBY_ANNOTATIONS = frozenset({"text", "textContainer", "delimiter"})


@dataclass(frozen=True)
class _Fragment:
    """保存主唱文本片段及来源明确提供的绝对毫秒时轴。"""

    text: str
    start: Optional[int] = None
    end: Optional[int] = None


@dataclass(frozen=True)
class _Line:
    """保留完整主唱句子，只有全部词语有可靠时轴时才提供逐词数据。"""

    text: str
    start: Optional[int]
    end: Optional[int]
    words: tuple[_Fragment, ...]


def _load_document(content: str) -> Optional[etree._Element]:
    """限制外部 XML 的体积、结构和实体能力，解析失败时拒绝整个文档。"""
    if not content or len(content) > _MAX_BYTES:
        return None
    try:
        data = content.encode("utf-8")
        if len(data) > _MAX_BYTES:
            return None
        parser = etree.XMLParser(
            encoding="utf-8", resolve_entities=False, no_network=True,
            load_dtd=False, remove_comments=True, remove_pis=True,
        )
        root = etree.fromstring(data, parser=parser)
    except (etree.XMLSyntaxError, UnicodeError, ValueError):
        return None
    if root.tag != f"{_TTML}tt" or root.getroottree().docinfo.doctype:
        return None
    return root if _bounded_tree(root) else None


def _bounded_tree(root: etree._Element) -> bool:
    """按开始和结束事件计算深度及节点数，限制后续递归转换的资源消耗。"""
    depth = 0
    count = 0
    for event, _element in etree.iterwalk(root, events=("start", "end")):
        if event == "end":
            depth -= 1
            continue
        depth += 1
        count += 1
        if depth > _MAX_DEPTH or count > _MAX_NODES:
            return False
    return True


def _timestamp(value: Optional[str]) -> Optional[int]:
    """解析 AMLL 的秒、分秒或时分秒格式，不接受单位后缀和帧计时。"""
    text = (value or "").strip()
    if not text or len(text) > 32 or not _TIMESTAMP.fullmatch(text):
        return None
    integer, _, fraction = text.partition(".")
    seconds = 0
    for part in integer.split(":"):
        seconds = seconds * 60 + int(part)
    # 使用十进制整数截取毫秒，避免浮点数把已有时轴偏移一个毫秒。
    return seconds * 1000 + int((fraction + "000")[:3])


def _time_range(element: etree._Element) -> tuple[Optional[int], Optional[int]]:
    """读取绝对起止时轴；AMLL 子节点时间不叠加父节点的开始时间。"""
    start = _timestamp(element.get("begin"))
    end = _timestamp(element.get("end"))
    if start is None or end is None or end <= start:
        return None, None
    return start, end


def _is_auxiliary(element: etree._Element) -> bool:
    """排除翻译、音译、背景人声以及 Ruby 注音文本，保留主唱和 Ruby 基字。"""
    roles = set((element.get(f"{_METADATA}role") or "").split())
    return bool(roles & _AUXILIARY_ROLES) or (
        element.get(f"{_STYLING}ruby") in _RUBY_ANNOTATIONS
    )


def _text(value: Optional[str]) -> str:
    """忽略纯换行缩进，同时保留词内、词尾和同行片段间的必要空格。"""
    if not value or ("\n" in value and not value.strip()):
        return ""
    return re.sub(r"\s+", " ", value)


def _fragments(element: etree._Element) -> list[_Fragment]:
    """按 XML 混合内容顺序提取主唱片段，不把排除节点的正文混入歌词。"""
    parts = [_Fragment(_text(element.text))]
    for child in element:
        if not _is_auxiliary(child):
            if child.tag == f"{_TTML}span":
                parts.extend(_span_fragments(child))
            elif child.tag == f"{_TTML}br":
                parts.append(_Fragment(" "))
        parts.append(_Fragment(_text(child.tail)))
    return [part for part in parts if part.text]


def _span_fragments(element: etree._Element) -> list[_Fragment]:
    """保留最细的可用词时轴，纯样式嵌套则归入外层已有的词时轴。"""
    parts = _fragments(element)
    start, end = _time_range(element)
    if start is None or any(part.start is not None for part in parts):
        return parts
    text = "".join(part.text for part in parts)
    return [_Fragment(text, start, end)] if text else []


def _complete_words(parts: list[_Fragment]) -> tuple[_Fragment, ...]:
    """仅在主唱文本完整受词时轴覆盖时生成逐词列表，并保留词间空白。"""
    words: list[_Fragment] = []
    for part in parts:
        if not part.text.strip():
            if words:
                words[-1] = replace(words[-1], text=words[-1].text + part.text)
        elif part.start is None or part.end is None:
            return ()
        else:
            words.append(part)
    if words:
        words[0] = replace(words[0], text=words[0].text.lstrip())
        words[-1] = replace(words[-1], text=words[-1].text.rstrip())
    return tuple(words)


def _line(element: etree._Element) -> Optional[_Line]:
    """构造完整歌词行，缺少行时间时只从完整逐词时轴恢复行范围。"""
    parts = _fragments(element)
    text = "".join(part.text for part in parts).strip()
    if not text:
        return None
    words = _complete_words(parts)
    start, end = _time_range(element)
    if not element.get("begin") and not element.get("end") and words:
        start = min(word.start for word in words if word.start is not None)
        end = max(word.end for word in words if word.end is not None)
    if not _words_fit_line(words, start, end):
        words = ()
    return _Line(text, start, end, words)


def _words_fit_line(
    words: tuple[_Fragment, ...], start: Optional[int], end: Optional[int],
) -> bool:
    """只保留完全位于行时间范围内的逐词时轴，不夹断来源时间。"""
    if start is None or end is None:
        return False
    return all(
        word.start is not None and word.end is not None
        and start <= word.start < word.end <= end
        for word in words
    )


def _main_lines(body: etree._Element) -> list[_Line]:
    """按正文顺序读取歌词行，同时排除整个辅助轨道容器下的行。"""
    lines = []
    for element in body.iter(f"{_TTML}p"):
        if _is_auxiliary(element) or any(
            _is_auxiliary(parent) for parent in element.iterancestors()
        ):
            continue
        line = _line(element)
        if line:
            lines.append(line)
    return lines


def _line_payload(line: _Line) -> dict[str, object]:
    """生成 Lyricsfile 1.0 行结构，仅在有效逐词时轴存在时写入 words。"""
    payload: dict[str, object] = {
        "start_ms": line.start,
        "end_ms": line.end,
        "text": line.text,
    }
    if line.words:
        payload["words"] = [
            {"start_ms": word.start, "end_ms": word.end, "text": word.text}
            for word in line.words
        ]
    return payload


def parse_lyrics(
    content: str, *, title: str, artist: str, provider_id: str, match_score: int,
) -> Optional[MusicLyrics]:
    """转换已匹配的 AMLL TTML；不完整行时轴整份降为纯文本且不推测音轨时长。"""
    root = _load_document(content)
    if root is None or not title.strip() or not artist.strip():
        return None
    body = root.find(f"{_TTML}body")
    if body is None:
        return None
    lines = _main_lines(body)
    if not lines:
        return None
    language = root.get(f"{_XML}lang") or None
    lyrics = MusicLyrics(
        provider="amll", provider_id=provider_id, match_score=match_score,
        language=language,
    )
    if any(line.start is None or line.end is None for line in lines):
        lyrics.plain_lyrics = "\n".join(line.text for line in lines)
        return lyrics
    payload: dict[str, object] = {
        "version": "1.0",
        "metadata": {"title": title, "artist": artist, "language": language},
        "lines": [_line_payload(line) for line in lines],
    }
    serialized = _serialize_payload(payload)
    if len(serialized.encode("utf-8")) > _MAX_BYTES:
        return None
    return _lyricsfile_result(lyrics, serialized)


def _serialize_payload(payload: dict[str, object]) -> str:
    """使用已锁定且带类型声明的安全 YAML 序列化器生成标准 Lyricsfile。"""
    serializer = YAML(typ="safe")
    # 宿主使用 YAML 1.1 读者；按同一规则引用 No、on 等歌词，避免被解析为布尔值。
    serializer.version = (1, 1)
    serializer.allow_unicode = True
    serializer.default_flow_style = False
    with StringIO() as stream:
        serializer.dump(payload, stream)
        return stream.getvalue()


def _lyricsfile_result(lyrics: MusicLyrics, serialized: str) -> Optional[MusicLyrics]:
    """通过宿主模型校验并派生 LRC 与纯文本，拒绝不能安全派生的输出。"""
    result = replace(lyrics, lyricsfile=serialized)
    return result if result.content else None
