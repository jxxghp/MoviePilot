"""
验证字幕下载链接的解析与规范化。
"""

from lxml import etree

from app.modules.subtitle import SubtitleModule


def test_parse_subtitle_links_filters_detail_page_action_links():
    """
    字幕链接解析只保留下载地址，排除详情页中的其他操作链接。
    """
    html_text = """
    <tr>
      <td class="rowhead" valign="top">字幕</td>
      <td class="rowfollow" align="left" valign="top">
        <table border="0" cellspacing="0">
          <tbody>
            <tr>
              <td class="embedded">
                <img border="0" src="pic/flag/china.gif" alt="简体中文" title="简体中文">
              </td>
              <td class="embedded">
                <a href="downloadsubs.php?torrentid=621182&amp;subid=2148">
                  <u>[雪飘+白恋] 光之美少女 All Stars NewStage：未来的朋友</u>
                </a>
              </td>
            </tr>
          </tbody>
        </table>
        <form method="post" action="subtitles.php">
          <a href="javascript:void(0)" onclick="this.closest(&quot;form&quot;).submit()">上传字幕</a>
        </form>
        <form method="get" action="http://shooter.cn/sub/" target="_blank">
          <a href="javascript:void(0)" onclick="this.closest(&quot;form&quot;).submit()">搜索射手网</a>
        </form>
        <form method="get" action="https://www.opensubtitles.org/en/search2/" target="_blank">
          <a href="javascript:void(0)" onclick="this.closest('form').submit()">搜索Opensubtitles</a>
        </form>
      </td>
    </tr>
    """
    html = etree.HTML(html_text)

    links = SubtitleModule._parse_subtitle_links(
        html, "https://audiences.me/details.php?id=599860&hit=1"
    )

    assert links == [
        "https://audiences.me/downloadsubs.php?torrentid=621182&subid=2148"
    ]
