"""AMLL TTML 的主唱提取、时轴转换和外部 XML 边界测试。"""

import pytest
import yaml

from app.domain.context import MusicLyrics
from app.modules.amll.lyrics import parse_lyrics


def _document(body: str, *, head: str = "", root_attributes: str = "") -> str:
    """用合成歌词构造符合 AMLL 命名空间的测试文档，避免访问真实歌词源。"""
    return (
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttm="http://www.w3.org/ns/ttml#metadata" '
        'xmlns:tts="http://www.w3.org/ns/ttml#styling" '
        'xmlns:itunes="http://itunes.apple.com/lyric-ttml-extensions" '
        f'xml:lang="en" {root_attributes}>'
        f'<head>{head}</head><body dur="05:00.000"><div>{body}</div></body></tt>'
    )


def _parse(content: str):
    """固定已经由来源匹配层确认的元数据，直接测试纯格式转换入口。"""
    return parse_lyrics(
        content, title="Example Track", artist="Example Artist",
        provider_id="123", match_score=95,
    )


def test_word_ttml_preserves_times_spaces_and_removes_auxiliary_tracks() -> None:
    """逐词歌词保留主唱绝对时轴和英文空格，不混入三类辅助声音或头部翻译。"""
    lyrics = _parse(_document(
        '<p begin="1.250" end="3.000">'
        '<span ttm:role="x-bg" begin="1" end="2">Background first</span>'
        '<span begin="1.250" end="2">Example</span> '
        '<span begin="2" end="3">line</span>'
        '<span ttm:role="x-translation">翻译示例</span>'
        '<span ttm:role="x-roman">Romanized example</span>'
        '<span ttm:role="x-bg"><span begin="2" end="3">Backing</span></span>'
        '</p>',
        head='<metadata><translation><p begin="1" end="3">Sidecar</p>'
        '</translation></metadata>',
    ))

    assert lyrics is not None
    assert lyrics.provider == "amll"
    assert lyrics.provider_id == "123"
    assert lyrics.match_score == 95
    assert lyrics.language == "en"
    assert lyrics.synced_lyrics == "[00:01.25]Example line"
    assert lyrics.plain_lyrics == "Example line"
    assert lyrics.quality_rank == 4
    payload = yaml.safe_load(lyrics.lyricsfile)
    assert payload["version"] == "1.0"
    assert payload["lines"] == [{
        "start_ms": 1250, "end_ms": 3000, "text": "Example line",
        "words": [
            {"start_ms": 1250, "end_ms": 2000, "text": "Example "},
            {"start_ms": 2000, "end_ms": 3000, "text": "line"},
        ],
    }]
    assert "duration" not in payload["metadata"]


@pytest.mark.parametrize(("begin", "end", "expected"), [
    ("1.001", "2.501", 1001),
    ("01:02.345", "01:03.456", 62345),
    ("1:02:03.456", "1:02:04.567", 3723456),
    ("120", "121", 120000),
    ("0", "0.5", 0),
    (" 1.250 ", "2", 1250),
])
def test_line_ttml_parses_official_timestamp_forms(begin, end, expected) -> None:
    """秒、分秒和时分秒格式统一为整数毫秒，逐行内容不虚报逐词质量。"""
    lyrics = _parse(_document(f'<p begin="{begin}" end="{end}">Example line</p>'))

    assert lyrics is not None
    assert lyrics.quality_rank == 3
    payload = yaml.safe_load(lyrics.lyricsfile)
    assert payload["lines"][0]["start_ms"] == expected
    assert "words" not in payload["lines"][0]


def test_indentation_does_not_insert_spaces_between_chinese_words() -> None:
    """格式化 XML 的换行缩进不应拆开无词间空格的中文歌词。"""
    lyrics = _parse(_document('''
        <p begin="1" end="3">
            <span begin="1" end="2">示</span>
            <span begin="2" end="3">例</span>
        </p>
    '''))

    assert lyrics is not None
    assert lyrics.plain_lyrics == "示例"
    assert lyrics.quality_rank == 4


def test_word_internal_spaces_survive_formatted_document() -> None:
    """英文词尾空格放在 span 内时，即使文件格式化也应完整保留。"""
    lyrics = _parse(_document('''
        <p begin="1" end="3">
            <span begin="1" end="2">Example </span>
            <span begin="2" end="3">line</span>
        </p>
    '''))

    assert lyrics is not None
    assert lyrics.plain_lyrics == "Example line"
    assert yaml.safe_load(lyrics.lyricsfile)["lines"][0]["words"][0]["text"] == "Example "


def test_nested_styles_keep_finest_word_times_and_exclude_ruby_annotations() -> None:
    """嵌套样式不重复文本，Ruby 注音不混入主唱句子，词级时轴保持最细粒度。"""
    lyrics = _parse(_document(
        '<p begin="10" end="12"><span begin="10" end="12">'
        '<span begin="10" end="11"><span tts:ruby="base">示</span>'
        '<span tts:ruby="textContainer"><span tts:ruby="text">shi</span></span></span>'
        '<span begin="11" end="12">例</span></span></p>'
    ))

    assert lyrics is not None
    assert lyrics.plain_lyrics == "示例"
    assert lyrics.quality_rank == 4
    assert len(yaml.safe_load(lyrics.lyricsfile)["lines"][0]["words"]) == 2


def test_word_times_can_supply_missing_line_time() -> None:
    """完整逐词数据可以恢复缺失的行范围，不把正文开始时间累加到词时间。"""
    lyrics = _parse(_document(
        '<p><span begin="10" end="11">Example </span>'
        '<span begin="11" end="12">line</span></p>'
    ).replace('<div>', '<div begin="10" end="20">'))

    assert lyrics is not None
    assert lyrics.synced_lyrics == "[00:10.00]Example line"
    assert lyrics.quality_rank == 4


@pytest.mark.parametrize("word", [
    '<span begin="2" end="1">line</span>',
    '<span begin="1" end="5">line</span>',
    '<span begin="1">line</span>',
    '<span begin="1s" end="2s">line</span>',
    '<span>line</span>',
])
def test_invalid_or_partial_word_timing_falls_back_to_complete_line(word) -> None:
    """无效、越界或缺失的部分词时间不得虚报逐词质量，也不能丢失正文。"""
    lyrics = _parse(_document(
        '<p begin="1" end="3"><span begin="1" end="2">Example </span>'
        f'{word}</p>'
    ))

    assert lyrics is not None
    assert lyrics.plain_lyrics == "Example line"
    assert lyrics.quality_rank == 3
    assert "words" not in yaml.safe_load(lyrics.lyricsfile)["lines"][0]


@pytest.mark.parametrize("attributes", [
    '', 'begin="2" end="1"', 'begin="-1" end="2"', 'begin="NaN" end="2"',
    'begin="1s" end="2s"', 'begin="1:2:3:4" end="5"', 'begin="1"',
])
def test_any_untimed_line_falls_back_to_plain_without_losing_other_lines(attributes) -> None:
    """混合无效和有效行时轴时整份降为纯文本，保留完整文本而不伪造零秒时间。"""
    lyrics = _parse(_document(
        f'<p {attributes}>First line</p><p begin="3" end="4">Second line</p>'
    ))

    assert lyrics is not None
    assert lyrics.plain_lyrics == "First line\nSecond line"
    assert lyrics.synced_lyrics is None
    assert lyrics.lyricsfile is None
    assert lyrics.quality_rank == 1


def test_auxiliary_containers_and_empty_lines_are_ignored() -> None:
    """只有辅助人声和空行的条目无候选，完整辅助容器也不贡献正文。"""
    assert _parse(_document('<p begin="1" end="2"><span ttm:role="x-bg">Backing</span></p>')) is None
    lyrics = _parse(_document(
        '<div ttm:role="x-translation"><p begin="1" end="2">Translation</p></div>'
        '<p begin="1" end="2"> </p><p begin="2" end="3">Main</p>'
    ))
    assert lyrics is not None
    assert lyrics.plain_lyrics == "Main"


def test_xml_entities_and_doctypes_are_rejected_without_resolution(tmp_path) -> None:
    """任何 DTD 都被拒绝，外部文件实体不能泄露内容，网络实体不能触发请求。"""
    secret = tmp_path / "secret.txt"
    secret.write_text("should never appear", encoding="utf-8")
    for source in (secret.as_uri(), "https://example.invalid/secret"):
        document = '<!DOCTYPE tt [<!ENTITY payload SYSTEM "' + source + '">]>'
        document += _document('<p begin="1" end="2">&payload;</p>')
        assert _parse(document) is None
    assert _parse('<!DOCTYPE tt [<!ENTITY value "expanded">]>' + _document(
        '<p begin="1" end="2">&value;</p>'
    )) is None


@pytest.mark.parametrize("content", [
    '', '<tt>', '<html><body><p>Wrong format</p></body></html>',
    '<tt xmlns="http://www.w3.org/ns/ttml"><head/></tt>',
    '<tt xmlns="urn:wrong"><body><p>Wrong namespace</p></body></tt>',
    '\ud800',
])
def test_malformed_empty_or_non_ttml_documents_return_no_candidate(content) -> None:
    """无正文、非 TTML、损坏 XML 和不可编码文本不应产生可写入候选。"""
    assert _parse(content) is None


def test_size_depth_and_node_limits_reject_oversized_documents() -> None:
    """在转换前限制字节数、深度和节点数，避免外部歌词放大解析成本。"""
    assert _parse(_document('<p>' + 'a' * (1024 * 1024) + '</p>')) is None
    assert _parse(_document('<p>' + '中' * 400000 + '</p>')) is None
    assert _parse(_document('<span>' * 21 + '<p>Deep</p>' + '</span>' * 21)) is None
    assert _parse(_document('<p>Line</p>' * 10001)) is None


def test_existing_xml_declaration_and_escaped_text_are_decoded_safely() -> None:
    """已解码字符串的声明不会重解释 UTF-8 字节，标准转义字符正常还原。"""
    content = '<?xml version="1.0" encoding="ISO-8859-1"?>' + _document(
        '<p begin="1" end="2">示例 &amp; text</p>'
    )
    lyrics = _parse(content)

    assert lyrics is not None
    assert lyrics.plain_lyrics == "示例 & text"


def test_missing_confirmed_identity_is_rejected() -> None:
    """转换器不为缺少已匹配标题或艺人的调用伪造 Lyricsfile 元数据。"""
    content = _document('<p begin="1" end="2">Example line</p>')
    assert parse_lyrics(content, title="", artist="Artist", provider_id="1", match_score=90) is None
    assert parse_lyrics(content, title="Track", artist=" ", provider_id="1", match_score=90) is None


@pytest.mark.parametrize("text", [
    "No", "on", "yes", "Off", "TRUE", "false", "null", "Null", "~",
    "123", "0123", "1.25", "01:02", "2026-09-09",
])
def test_lyricsfile_roundtrip_preserves_yaml_scalar_looking_lyrics(text: str) -> None:
    """歌词词语不能被宿主 YAML 1.1 读者误解为布尔、空值、数字或日期。"""
    content = _document(
        f'<p begin="1" end="2"><span begin="1" end="2">{text}</span></p>'
    )
    lyrics = _parse(content)

    assert lyrics is not None
    assert lyrics.plain_lyrics == text
    assert lyrics.quality_rank == 4
    # 再经过真实宿主模型读取生成物，覆盖写入旁挂后重新读取的类型合同。
    restored = MusicLyrics(provider="amll", lyricsfile=lyrics.lyricsfile)
    assert restored.plain_lyrics == text
    assert restored.synced_lyrics == f"[00:01.00]{text}"
    payload = yaml.safe_load(lyrics.lyricsfile)
    assert payload["lines"][0]["text"] == text
    assert payload["lines"][0]["words"][0]["text"] == text
    assert payload["lines"][0]["start_ms"] == 1000
    assert isinstance(payload["lines"][0]["start_ms"], int)


def test_lyricsfile_roundtrip_preserves_scalar_looking_identity_and_null_language() -> None:
    """元数据名称仍为字符串，缺失语言仍为 null，不通过全字段字符串化修复。"""
    content = _document('<p begin="1" end="2">Example line</p>').replace('xml:lang="en"', '')
    lyrics = parse_lyrics(content, title="On", artist="No", provider_id="1", match_score=90)

    assert lyrics is not None
    restored = MusicLyrics(provider="amll", lyricsfile=lyrics.lyricsfile)
    assert restored.plain_lyrics == "Example line"
    assert restored.language is None
    payload = yaml.safe_load(lyrics.lyricsfile)
    assert payload["version"] == "1.0"
    assert payload["metadata"] == {"title": "On", "artist": "No", "language": None}
