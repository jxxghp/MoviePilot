from types import SimpleNamespace

from app.helper import rss as rss_module
from app.helper.rss import RssHelper
from app.utils.http import RequestUtils


def test_rss_helper_decodes_utf8_xml_before_python_parser(monkeypatch):
    """
    RSS 解码应先修正 XML 文本，再交给 Python 解析兜底路径处理。
    """
    xml = """
    <?xml version="1.0" encoding="UTF-8"?>
    <rss>
      <channel>
        <item>
          <title><![CDATA[警察故事4：简单任务 2160p]]></title>
          <description><![CDATA[中文简介]]></description>
          <link>https://example.com/details/4</link>
          <pubDate>2026-06-25T10:30:00Z</pubDate>
        </item>
      </channel>
    </rss>
    """.strip()

    class FakeRequestUtils:
        """
        测试用 RequestUtils，避免真实网络请求。
        """

        get_decoded_xml_content = staticmethod(RequestUtils.get_decoded_xml_content)

        def __init__(self, **_kwargs):
            """
            保存构造参数占位，兼容 RssHelper 的调用方式。
            """

        def get_res(self, _url):
            """
            返回带错误 HTTP 默认编码的 RSS 响应对象。
            """
            return SimpleNamespace(
                status_code=200,
                content=xml.encode("utf-8"),
                text=xml.encode("utf-8").decode("ISO-8859-1"),
                apparent_encoding="utf-8",
                encoding="ISO-8859-1",
            )

    monkeypatch.setattr(rss_module, "RequestUtils", FakeRequestUtils)
    monkeypatch.setattr(rss_module.rust_accel, "parse_rss_items", lambda *_args, **_kwargs: None)

    result = RssHelper().parse("https://example.com/rss")

    assert result[0]["title"] == "警察故事4：简单任务 2160p"
    assert result[0]["description"] == "中文简介"
