"""
按已知页面标题和选择器识别人机验证页面，不执行挑战破解或浏览器操作。
"""

import os

from pyquery import PyQuery

from app.runtime.log import logger

CHALLENGE_TITLES = [
    # Cloudflare 标准挑战特征
    'Just a moment...',
    '请稍候…',
    # DDoS-GUARD 防护页特征
    'DDOS-GUARD',
]
CHALLENGE_SELECTORS = [
    # Cloudflare 标准挑战特征
    '#cf-challenge-running', '.ray_id', '.attack-box', '#cf-please-wait', '#challenge-spinner', '#trk_jschal_js',
    # EbookParadijs、Film-Paleis、MuziekFabriek 与 Puur-Hollands 使用的自定义挑战页
    'td.info #js_info',
    # Fairlane 与 pararius.com 使用的挑战页
    'div.vc div.text-box h2'
]
# 短等待间隔，单位为秒，保留旧调用方使用的配置常量。
SHORT_TIMEOUT = 6
# 挑战页等待上限，单位为秒；沿用 NASTOOL_CF_TIMEOUT 环境变量。
CF_TIMEOUT = int(os.getenv("NASTOOL_CF_TIMEOUT", "60"))


def under_challenge(html_text: str) -> bool:
    """
    检查 HTML 是否命中已知的人机验证页面特征。

    :param html_text: 待检查的网页源码
    :return: 标题或选择器命中时返回 True；空源码或未命中时返回 False
    """
    if not html_text:
        return False
    page_title = PyQuery(html_text)('title').text()
    logger.debug("under_challenge page_title=" + page_title)
    for title in CHALLENGE_TITLES:
        if page_title.lower() == title.lower():
            return True
    for selector in CHALLENGE_SELECTORS:
        html_doc = PyQuery(html_text)
        if html_doc(selector):
            return True
    return False
