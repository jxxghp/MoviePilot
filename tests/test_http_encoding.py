from types import SimpleNamespace

from app.utils.http import RequestUtils


def test_xml_decoding_prefers_xml_declaration_over_http_default():
    """
    XML 声明应优先于 HTTP 默认编码，避免 UTF-8 RSS 标题被 latin1 类编码解坏。
    """
    xml = '<?xml version="1.0" encoding="UTF-8"?><rss><title>警察故事4：简单任务</title></rss>'
    response = SimpleNamespace(
        content=xml.encode("utf-8"),
        encoding="ISO-8859-1",
        apparent_encoding="utf-8",
        text=xml.encode("utf-8").decode("ISO-8859-1"),
    )

    decoded = RequestUtils.get_decoded_xml_content(response, performance_mode=True)

    assert "警察故事4：简单任务" in decoded
    assert "è­¦" not in decoded


def test_xml_decoding_uses_declared_non_utf8_encoding():
    """
    XML 声明为非 UTF-8 时应按声明解码，兼容旧站点的 GBK/Big5 类响应。
    """
    xml = '<?xml version="1.0" encoding="GBK"?><rss><title>中文标题</title></rss>'
    response = SimpleNamespace(
        content=xml.encode("gbk"),
        encoding="ISO-8859-1",
        apparent_encoding="ISO-8859-1",
        text=xml.encode("gbk").decode("ISO-8859-1"),
    )

    decoded = RequestUtils.get_decoded_xml_content(response, performance_mode=True)

    assert "中文标题" in decoded


def test_xml_decoding_skips_low_confidence_apparent_encoding():
    """
    apparent_encoding 为 latin1 类编码时不应抢先解码，避免无 XML 声明的中文 RSS 被吞成乱码。
    """
    xml = "<rss><title>中文标题</title></rss>"
    response = SimpleNamespace(
        content=xml.encode("gbk"),
        encoding="ISO-8859-1",
        apparent_encoding="ISO-8859-1",
        text=xml.encode("gbk").decode("ISO-8859-1"),
    )

    decoded = RequestUtils.get_decoded_xml_content(response, performance_mode=True)

    assert "中文标题" in decoded
    assert "ÖÐÎÄ" not in decoded


def test_latin1_http_encoding_is_low_confidence():
    """
    latin1 类编码常由 HTTP 客户端默认填充，不能作为 XML/RSS 解码的优先依据。
    """
    assert RequestUtils.is_low_confidence_http_encoding("ISO-8859-1")
    assert RequestUtils.is_low_confidence_http_encoding("latin-1")
    assert not RequestUtils.is_low_confidence_http_encoding("utf-8")
