"""按配置生成可控的网络连通性探测目标目录。"""

from collections.abc import Callable, Collection, Mapping
from typing import Any, Optional
from urllib.parse import urlparse

from app.foundation.url import UrlUtils

from .domain import NetworkTestRule, SettingsReader


def _configured_service_rule(
    *,
    target_id: str,
    url: str,
    icon: str,
    proxy: bool,
    module_ids: tuple[str, ...] = (),
    name_suffix: Optional[str] = None,
    request_path: str = "",
) -> Optional[NetworkTestRule]:
    """为 HTTPS 服务生成隐藏真实路径的目录项和同源跳转范围。"""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        parsed.port
    except ValueError:
        return None
    host_label = _address_label(url) or hostname
    base_url = url if url.endswith("/") else f"{url}/"
    target_url = f"{base_url}{request_path.lstrip('/')}"
    redirect_prefix = f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"
    if not redirect_prefix.endswith("/"):
        redirect_prefix = f"{redirect_prefix}/"
    return NetworkTestRule(
        id=target_id,
        name=name_suffix or host_label,
        icon=icon,
        url=target_url,
        proxy=proxy,
        allowed_redirect_prefixes=(redirect_prefix,),
        module_ids=module_ids,
    )


def _address_label(url: str) -> Optional[str]:
    """从 URL 中提取不含凭据的主机和显式端口作为目标名称。"""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    if port and port != 443:
        return f"{hostname}:{port}"
    return hostname


def _freeze_headers(value: Any) -> tuple[tuple[str, str], ...]:
    """把动态配置中的请求头复制为不可变、可比较的规则字段。"""
    if not isinstance(value, Mapping):
        return ()
    return tuple((str(key), str(item)) for key, item in value.items() if item is not None)


def build_network_rules(
    settings: SettingsReader,
    enabled_module_ids_reader: Optional[Callable[[], Collection[str]]],
) -> tuple[NetworkTestRule, ...]:
    """组合外部模块、代理和固定服务目标，并按模块开关裁剪目录。"""
    enabled_module_ids = set(enabled_module_ids_reader()) if enabled_module_ids_reader is not None else None
    github_proxy = UrlUtils.standardize_base_url(settings("GITHUB_PROXY", None) or "")
    pip_proxy = UrlUtils.standardize_base_url(settings("PIP_PROXY", None) or "https://pypi.org/simple/")
    rules = _build_network_proxy_rules(settings, github_proxy, pip_proxy)
    rules.extend(_build_metadata_module_rules(settings))
    rules.extend(_build_notification_module_rules())
    rules.extend(_build_external_media_rules(settings))
    rules.extend(_build_music_service_rules())
    rules.extend(_build_configured_module_rules(settings))
    rules.extend(_build_fixed_service_rules(settings, github_proxy))
    if enabled_module_ids is None:
        return tuple(rules)
    return tuple(rule for rule in rules if not rule.module_ids or enabled_module_ids.intersection(rule.module_ids))


def _build_metadata_module_rules(settings: SettingsReader) -> list[NetworkTestRule]:
    """构建 TMDB、TVDB、Fanart 和豆瓣等启用时使用的媒体服务目标。"""
    tmdb_key = settings("TMDB_API_KEY", None)
    tmdb_domain = settings("TMDB_API_DOMAIN", None) or "api.themoviedb.org"
    rules = [
        NetworkTestRule(
            id="tmdb_api",
            name="TMDB API",
            icon="tmdb",
            url=f"https://api.themoviedb.org/3/movie/550?api_key={tmdb_key}",
            proxy=True,
            allowed_redirect_prefixes=("https://api.themoviedb.org/3/",),
            module_ids=("TheMovieDbModule",),
        ),
        NetworkTestRule(
            id="tmdb_api_alt",
            name="TMDB API 备用域名",
            icon="tmdb",
            url=f"https://api.tmdb.org/3/movie/550?api_key={tmdb_key}",
            proxy=True,
            allowed_redirect_prefixes=("https://api.tmdb.org/3/",),
            module_ids=("TheMovieDbModule",),
        ),
        NetworkTestRule(
            id="tmdb_web",
            name="TMDB 网站",
            icon="tmdb",
            url="https://www.themoviedb.org",
            proxy=True,
            allowed_redirect_prefixes=("https://www.themoviedb.org/",),
            module_ids=("TheMovieDbModule",),
        ),
        NetworkTestRule(
            id="tvdb_api",
            name="TVDB API",
            icon="tvdb",
            url="https://api4.thetvdb.com/v4/series/81189",
            proxy=True,
            allowed_redirect_prefixes=("https://api4.thetvdb.com/v4/",),
            module_ids=("TheTvDbModule",),
            success_status_codes=(200, 401),
        ),
        NetworkTestRule(
            id="fanart_api",
            name="Fanart 图库服务",
            icon="fanart",
            url="https://webservice.fanart.tv",
            proxy=True,
            allowed_redirect_prefixes=("https://webservice.fanart.tv/",),
            module_ids=("FanartModule",),
        ),
        NetworkTestRule(
            id="douban_api",
            name="豆瓣媒体服务",
            icon="douban",
            url="https://frodo.douban.com",
            proxy=False,
            allowed_redirect_prefixes=(
                "https://frodo.douban.com/",
                "https://www.douban.com/doubanapp/frodo",
            ),
            module_ids=("DoubanModule",),
        ),
        NetworkTestRule(
            id="douban_web",
            name="豆瓣网页服务",
            icon="douban",
            url="https://movie.douban.com",
            proxy=True,
            allowed_redirect_prefixes=("https://movie.douban.com/",),
            module_ids=("DoubanModule",),
        ),
    ]
    if tmdb_domain not in {"api.themoviedb.org", "api.tmdb.org"}:
        rules.insert(
            2,
            NetworkTestRule(
                id="tmdb_api_configured",
                name="TMDB API 自定义域名",
                icon="tmdb",
                url=f"https://{tmdb_domain}/3/movie/550?api_key={tmdb_key}",
                proxy=True,
                allowed_redirect_prefixes=(f"https://{tmdb_domain}/3/",),
                module_ids=("TheMovieDbModule",),
            ),
        )
    return rules


def _build_notification_module_rules() -> list[NetworkTestRule]:
    """构建已启用通知渠道对应的固定服务目标。"""
    return [
        NetworkTestRule(
            id="telegram_api",
            name="Telegram 通知",
            icon="telegram",
            url="https://api.telegram.org",
            proxy=True,
            allowed_redirect_prefixes=(
                "https://api.telegram.org/",
                "https://core.telegram.org/",
            ),
            module_ids=("TelegramModule",),
        ),
        NetworkTestRule(
            id="wechat_api",
            name="企业微信通知",
            icon="wechat",
            url="https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            proxy=False,
            allowed_redirect_prefixes=("https://qyapi.weixin.qq.com/",),
            module_ids=("WechatModule",),
        ),
        NetworkTestRule(
            id="wechat_bot_websocket",
            name="企业微信机器人长连接",
            icon="wechat",
            url="wss://openws.work.weixin.qq.com",
            proxy=False,
            allowed_redirect_prefixes=(),
            probe_protocol="websocket",
            module_ids=("WechatModule",),
        ),
        NetworkTestRule(
            id="slack_api",
            name="Slack 通知",
            icon="slack",
            url="https://slack.com",
            proxy=False,
            allowed_redirect_prefixes=(
                "https://slack.com/",
                "https://www.slack.com/",
            ),
            module_ids=("SlackModule",),
        ),
        NetworkTestRule(
            id="dingtalk_api",
            name="钉钉通知",
            icon="dingtalk",
            url="https://oapi.dingtalk.com",
            proxy=False,
            allowed_redirect_prefixes=("https://oapi.dingtalk.com/",),
            module_ids=("DingTalkModule",),
        ),
        NetworkTestRule(
            id="discord_api",
            name="Discord 通知",
            icon="discord",
            url="https://discord.com",
            proxy=True,
            allowed_redirect_prefixes=("https://discord.com/",),
            module_ids=("DiscordModule",),
        ),
        NetworkTestRule(
            id="feishu_api",
            name="飞书通知",
            icon="feishu",
            url="https://open.feishu.cn",
            proxy=False,
            allowed_redirect_prefixes=("https://open.feishu.cn/",),
            module_ids=("FeishuModule",),
        ),
        NetworkTestRule(
            id="qqbot_api",
            name="QQ 机器人 API",
            icon="qq",
            url="https://api.sgroup.qq.com/gateway",
            proxy=False,
            allowed_redirect_prefixes=("https://api.sgroup.qq.com/",),
            module_ids=("QQBotModule",),
            success_status_codes=(200, 400, 401, 403, 404, 405),
        ),
        NetworkTestRule(
            id="qqbot_auth",
            name="QQ 机器人授权服务",
            icon="qq",
            url="https://bots.qq.com/app/getAppAccessToken",
            proxy=False,
            allowed_redirect_prefixes=("https://bots.qq.com/",),
            http_method="POST",
            request_json=(),
            module_ids=("QQBotModule",),
            success_status_codes=(200, 400, 401, 403, 404, 405),
        ),
        NetworkTestRule(
            id="wechatclawbot_api",
            name="微信 ClawBot 服务",
            icon="wechat",
            url="https://ilinkai.weixin.qq.com/ilink/bot/getconfig",
            proxy=True,
            allowed_redirect_prefixes=("https://ilinkai.weixin.qq.com/",),
            http_method="POST",
            request_json=(),
            module_ids=("WechatClawBotModule",),
            success_status_codes=(200, 400, 401, 404, 405),
            display_address="https://ilinkai.weixin.qq.com",
        ),
        NetworkTestRule(
            id="wechatclawbot_cdn",
            name="微信 ClawBot 媒体 CDN",
            icon="wechat",
            url="https://novac2c.cdn.weixin.qq.com/c2c",
            proxy=True,
            allowed_redirect_prefixes=("https://novac2c.cdn.weixin.qq.com/",),
            module_ids=("WechatClawBotModule",),
            success_status_codes=(200, 403, 404),
        ),
    ]


def _build_external_media_rules(settings: SettingsReader) -> list[NetworkTestRule]:
    """构建 AniList、IMDb 与 AcoustID 等外部媒体来源探测目标。"""
    return [
        NetworkTestRule(
            id="acoustid_api",
            name="AcoustID 音频识别",
            icon="site",
            url=(f"https://api.acoustid.org/v2/lookup?client={settings('ACOUSTID_API_KEY', '')}&format=json"),
            proxy=True,
            allowed_redirect_prefixes=("https://api.acoustid.org/",),
            module_ids=("AcoustIdModule",),
            success_status_codes=(200, 400),
        ),
        NetworkTestRule(
            id="anilist_api",
            name="AniList 番剧元数据",
            icon="bangumi",
            url=("https://graphql.anilist.co/?query=%7B%20Media%28id%3A1%29%20%7Bid%7D%20%7D"),
            proxy=True,
            allowed_redirect_prefixes=("https://graphql.anilist.co/",),
            module_ids=("AniListModule",),
        ),
        NetworkTestRule(
            id="anilist_trace",
            name="trace.moe 番剧识别",
            icon="bangumi",
            url="https://trace.moe",
            proxy=True,
            allowed_redirect_prefixes=("https://trace.moe/",),
            module_ids=("AniListModule",),
        ),
        NetworkTestRule(
            id="imdb_api",
            name="IMDb 元数据",
            icon="tmdb",
            url="https://v2.sg.media-imdb.com/suggestion/x/inception.json",
            proxy=True,
            allowed_redirect_prefixes=("https://v2.sg.media-imdb.com/",),
            module_ids=("ImdbModule",),
        ),
        NetworkTestRule(
            id="imdb_graphql",
            name="IMDb GraphQL 服务",
            icon="tmdb",
            url="https://caching.graphql.imdb.com",
            proxy=True,
            allowed_redirect_prefixes=("https://caching.graphql.imdb.com/",),
            module_ids=("ImdbModule",),
        ),
    ]


def _build_music_service_rules() -> list[NetworkTestRule]:
    """构建 MusicBrainz、ListenBrainz 和 TheAudioDB 服务目标。"""
    return [
        NetworkTestRule(
            id="musicbrainz_api",
            name="MusicBrainz 音乐信息",
            icon="site",
            url=("https://musicbrainz.org/ws/2/recording?query=recording%3Aradiohead&fmt=json"),
            proxy=True,
            allowed_redirect_prefixes=("https://musicbrainz.org/",),
            module_ids=("MusicBrainzModule", "ListenBrainzModule"),
        ),
        NetworkTestRule(
            id="listenbrainz_api",
            name="ListenBrainz 音乐统计",
            icon="site",
            url=("https://api.listenbrainz.org/1/stats/sitewide/artists?range=all_time&count=1"),
            proxy=True,
            allowed_redirect_prefixes=("https://api.listenbrainz.org/",),
            module_ids=("ListenBrainzModule",),
        ),
        NetworkTestRule(
            id="theaudiodb_api",
            name="TheAudioDB 音乐信息",
            icon="site",
            url=("https://www.theaudiodb.com/api/v1/json/2/searchalbum.php?s=radiohead"),
            proxy=True,
            allowed_redirect_prefixes=("https://www.theaudiodb.com/",),
            module_ids=("TheAudioDbModule",),
        ),
    ]


def _build_configured_module_rules(settings: SettingsReader) -> list[NetworkTestRule]:
    """根据模块配置构建 Bangumi、封面、歌词和自定义代理目标。"""
    rules: list[NetworkTestRule] = []
    bangumi_proxy = bool(settings("BANGUMI_PROXY_ENABLE", False))
    bangumi_domain = (
        (settings("BANGUMI_API_DOMAIN", None) or "https://api.bgm.tv") if bangumi_proxy else "https://api.bgm.tv"
    )
    bangumi_rule = _configured_service_rule(
        target_id="bangumi_api",
        url=str(bangumi_domain or ""),
        icon="bangumi",
        proxy=True,
        module_ids=("BangumiModule",),
        name_suffix="Bangumi API",
    )
    if bangumi_rule:
        rules.append(bangumi_rule)

    configured_targets = (
        (
            "bangumi_image_proxy",
            "BANGUMI_IMAGE_DOMAIN",
            "bangumi",
            ("BangumiModule",),
            "",
            "Bangumi 图片代理",
        ),
        (
            "music_cover_proxy",
            "MUSIC_COVER_PROXY",
            "site",
            ("MusicBrainzModule", "ListenBrainzModule"),
            "",
            "音乐封面代理",
        ),
        (
            "lrclib_api",
            "LRCLIB_BASE_URL",
            "site",
            ("LrclibModule",),
            "/api/search?track_name=test",
            "LRCLIB 歌词服务",
        ),
        (
            "amll_api",
            "AMLL_BASE_URL",
            "site",
            ("AmllModule",),
            "/v1/lyrics/search?musicName=test&page=1&pageSize=1",
            "AMLL 歌词服务",
        ),
    )
    for target_id, setting_key, icon, module_ids, request_path, name in configured_targets:
        default_url = "https://coverartarchive.org" if target_id == "music_cover_proxy" else ""
        configured_url = str(settings(setting_key, default_url) or default_url).strip()
        if not configured_url:
            continue
        rule = _configured_service_rule(
            target_id=target_id,
            url=configured_url,
            icon=icon,
            proxy=True,
            module_ids=module_ids,
            name_suffix=name,
            request_path=request_path,
        )
        if rule:
            rules.append(rule)
    return rules


def _build_network_proxy_rules(
    settings: SettingsReader,
    github_proxy: str,
    pip_proxy: str,
) -> list[NetworkTestRule]:
    """构建全局代理、GitHub 与 Python 包镜像的连通性目标。"""
    rules: list[NetworkTestRule] = []
    network_proxy_rule = _build_network_proxy_rule(settings)
    if network_proxy_rule:
        rules.append(network_proxy_rule)

    configured_pip_proxy = settings("PIP_PROXY", None)
    rules.append(
        NetworkTestRule(
            id="pip_proxy",
            name="PIP 加速代理" if configured_pip_proxy else "PyPI 软件源",
            icon="python",
            url=f"{pip_proxy}rsa/",
            proxy=True,
            allowed_redirect_prefixes=(pip_proxy, "https://pypi.org/simple/"),
            expected_text="pypi:repository-version",
            invalid_message="PIP加速代理已失效，请检查配置",
            proxy_name="PIP加速代理",
        )
    )
    rules.extend(_build_github_proxy_rules(settings, github_proxy))
    return rules


def _build_network_proxy_rule(settings: SettingsReader) -> Optional[NetworkTestRule]:
    """用 GitHub API 请求验证当前配置的通用出站代理。"""
    proxy_config = settings("PROXY", None)
    proxy_address = settings("PROXY_HOST", None)
    if not proxy_address and isinstance(proxy_config, Mapping):
        proxy_address = proxy_config.get("https") or proxy_config.get("http")
    if not proxy_address:
        return None
    proxy_url = str(proxy_address).strip()
    if not (_address_label(proxy_url) or _address_label(f"http://{proxy_url}")):
        return None
    return NetworkTestRule(
        id="network_proxy",
        name="网络代理",
        icon="site",
        url="https://api.github.com",
        proxy=True,
        allowed_redirect_prefixes=("https://api.github.com/",),
        display_address=(proxy_url if "://" in proxy_url else f"http://{proxy_url}"),
    )


def _build_github_proxy_rules(
    settings: SettingsReader,
    github_proxy: str,
) -> list[NetworkTestRule]:
    """构建 GitHub 页面、API、源码和归档下载连通性目标。"""
    headers = _freeze_headers(settings("GITHUB_HEADERS", None))
    github_readme_url = "https://github.com/jxxghp/MoviePilot/blob/v2/README.md"
    raw_readme_url = "https://raw.githubusercontent.com/jxxghp/MoviePilot/v2/README.md"
    return [
        NetworkTestRule(
            id="github_proxy_web",
            name="GitHub 网页访问",
            icon="github",
            url=(f"{github_proxy}{github_readme_url}" if github_proxy else github_readme_url),
            proxy=True,
            allowed_redirect_prefixes=(
                "https://github.com/",
                *((f"{github_proxy}https://github.com/",) if github_proxy else ()),
            ),
            expected_text="MoviePilot",
            invalid_message=("Github加速代理已失效，请检查配置" if github_proxy else "无效响应"),
            proxy_name="Github加速代理" if github_proxy else None,
            headers=headers,
            display_address=github_proxy or github_readme_url,
        ),
        NetworkTestRule(
            id="github_api",
            name="GitHub API",
            icon="github",
            url="https://api.github.com",
            proxy=True,
            allowed_redirect_prefixes=("https://api.github.com/",),
            headers=headers,
        ),
        NetworkTestRule(
            id="github_codeload",
            name="GitHub 归档下载",
            icon="github",
            url="https://codeload.github.com",
            proxy=True,
            allowed_redirect_prefixes=(
                "https://codeload.github.com/",
                "https://github.com/",
            ),
            headers=headers,
        ),
        NetworkTestRule(
            id="github_proxy_raw",
            name="GitHub 源码下载",
            icon="github",
            url=(f"{github_proxy}{raw_readme_url}" if github_proxy else raw_readme_url),
            proxy=True,
            allowed_redirect_prefixes=(
                "https://raw.githubusercontent.com/",
                *((f"{github_proxy}https://raw.githubusercontent.com/",) if github_proxy else ()),
            ),
            expected_text="MoviePilot",
            invalid_message=("Github加速代理已失效，请检查配置" if github_proxy else "无效响应"),
            proxy_name="Github加速代理" if github_proxy else None,
            headers=headers,
            display_address=github_proxy or raw_readme_url,
        ),
    ]


def _build_fixed_service_rules(
    settings: SettingsReader,
    github_proxy: str,
) -> list[NetworkTestRule]:
    """保留插件市场和 MoviePilot 等非模块固定服务目标。"""
    rules: list[NetworkTestRule] = []
    plugin_repo_url = "https://github.com/jxxghp/MoviePilot-Plugins"
    plugin_repo_prefix = f"{plugin_repo_url}/"
    plugin_proxy_prefix = f"{github_proxy}{plugin_repo_prefix}" if github_proxy else None
    rules.append(
        NetworkTestRule(
            id="plugin_market",
            name="插件升级与安装",
            icon="github",
            url=(f"{github_proxy}{plugin_repo_url}" if github_proxy else plugin_repo_url),
            proxy=True,
            allowed_redirect_prefixes=(
                plugin_repo_prefix,
                *((plugin_proxy_prefix,) if plugin_proxy_prefix else ()),
            ),
            display_address=github_proxy or plugin_repo_url,
        )
    )

    services = (
        (
            "moviepilot_server",
            "MP_SERVER_HOST",
            "MoviePilot 中心服务",
            "https://movie-pilot.org",
        ),
        (
            "ocr_service",
            "OCR_HOST",
            "验证码识别服务",
            "https://movie-pilot.org",
        ),
        (
            "cookiecloud_service",
            "COOKIECLOUD_HOST",
            "CookieCloud 服务",
            "https://movie-pilot.org/cookiecloud",
        ),
        (
            "u115_auth_service",
            "U115_AUTH_SERVER",
            "115 授权服务",
            "https://movie-pilot.org",
        ),
    )
    for target_id, setting_key, service_name, default_url in services:
        if setting_key == "COOKIECLOUD_HOST" and (
            not settings("COOKIECLOUD_KEY", None)
            or not settings("COOKIECLOUD_PASSWORD", None)
            or settings("COOKIECLOUD_ENABLE_LOCAL", False)
        ):
            continue
        url = str(settings(setting_key, default_url) or "").strip()
        rule = _configured_service_rule(
            target_id=target_id,
            url=url,
            icon="site",
            proxy=True,
            name_suffix=service_name,
        )
        if rule:
            rules.append(rule)
    return rules
