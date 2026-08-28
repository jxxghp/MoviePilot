from types import SimpleNamespace

from app.adapters.network.http import RequestUtils
from app.application.rss import RssHelper, configure_rss_ports, reset_rss_ports


def test_rss_site_domain_prefers_configured_multilevel_domain():
    """RSS 站点域名匹配应保留配置中的多级域名，并为普通域名回退。"""
    assert RssHelper._get_site_domain("https://u2.dmhy.org/getrss.php") == "u2.dmhy.org"
    assert RssHelper._get_site_domain("https://tracker.example.com/rss") == "example.com"


def test_rss_helper_decodes_utf8_xml_before_python_parser():
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

    class FakeHttpPort:
        """
        测试用 RSS HTTP Port，避免真实网络请求。
        """

        def get(self, **_kwargs):
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

        @staticmethod
        def decode_xml(response, **kwargs):
            """复用真实编码策略验证 Port 边界前后的旧行为。"""
            return RequestUtils.get_decoded_xml_content(response, **kwargs)

    class FakeBrowserPort:
        """占位浏览器 Port，本用例不会触达。"""

        @staticmethod
        def render(**_kwargs):
            """拒绝意外浏览器访问。"""
            raise AssertionError("不应访问浏览器")

    class FakeParserPort:
        """关闭原生解析以验证 Python 解析兜底。"""

        @staticmethod
        def parse(_content, _max_items):
            """返回 None 触发 Python 解析。"""
            return None

    configure_rss_ports(
        http=FakeHttpPort(), browser=FakeBrowserPort(), parser=FakeParserPort()
    )

    try:
        result = RssHelper().parse("https://example.com/rss")
    finally:
        reset_rss_ports()

    assert result[0]["title"] == "警察故事4：简单任务 2160p"
    assert result[0]["description"] == "中文简介"
