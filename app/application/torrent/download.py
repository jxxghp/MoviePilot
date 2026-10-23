"""
种子下载、缓存、内容解析与候选匹配应用服务。

通过注入的 HTTP 端口访问站点，负责下载确认页面处理和种子失效判断。
"""

import datetime
import json
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, Union
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from lxml import html
from torrentool.api import Torrent
from torrentool.exceptions import TorrentoolException

from app.application.configuration import get_chain_runtime_config_snapshot, get_configured_system_config
from app.application.site.query import get_configured_site_query_service
from app.domain import torrent as torrent_rules
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import audio_quality_tier, normalize_audio_format, parse_audio_quality
from app.domain.metainfo import MetaInfo
from app.foundation import text as text_tools
from app.foundation.crypto import HashUtils
from app.foundation.url import UrlUtils
from app.runtime.cache import FileCache, TTLCache
from app.runtime.log import logger
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaSource, MediaType, SystemConfigKey

_SIZE_UNIT = 1024 * 1024


def _is_transient_torrent_status(status: int) -> bool:
    """
    403 和服务端错误可能来自站点前置代理，不能据此认定种子已失效。
    """
    return status == 403 or 500 <= status <= 599


class TorrentResponsePort(Protocol):
    """
    声明种子下载用例实际读取的 HTTP 响应字段。

    :ivar status_code: HTTP 响应状态码，用于重试与限流判断
    :ivar headers: 响应头，用于提取下载文件名等信息
    :ivar content: 响应原始字节，优先用于解析种子文件
    :ivar text: 解码后的响应正文，用于解析下载确认页面和错误
    :ivar reason: HTTP 状态的原因短语，用于失败诊断
    """

    status_code: int
    headers: dict[str, str]
    content: bytes
    text: str
    reason: str


def _torrent_rate_limit_error(response: Optional[TorrentResponsePort]) -> Optional[str]:
    """
    在种子解析前提取明确的 JSON 限流提示；HTTP 429 保留既有流控错误。
    """
    if response is None:
        return None
    content = response.content
    if content.lstrip().startswith(b"{"):
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeError):
            payload = {}
        message = payload.get("message") or payload.get("msg")
        if isinstance(message, str) and torrent_rules.rate_limit_cooldown(message) is not None:
            return f"站点限流：{message}"
    if response.status_code == 429:
        return "触发站点流控，请稍后重试"
    return None


class TorrentHttpPort(Protocol):
    """
    声明种子下载所需的同步 GET/POST 能力。
    """

    def request(self, *, method: str, url: str, cookie: Optional[str],
                ua: Optional[str], referer: Optional[str], proxies: Optional[dict[str, Any]],
                allow_redirects: bool = True,
                data: Optional[dict[str, Any]] = None) -> Optional[TorrentResponsePort]:
        """
        发送种子下载请求并返回窄响应。

        :param method: HTTP 请求方法，种子下载使用 GET 或 POST
        :param url: 种子下载或首次下载确认页面的地址
        :param cookie: 站点认证 Cookie；None 表示不附加 Cookie
        :param ua: 请求使用的用户代理；None 使用传输端默认值
        :param referer: 来源页面地址；None 表示不附加来源头
        :param proxies: 本次请求的代理映射；None 不显式配置代理
        :param allow_redirects: 是否跟随 HTTP 重定向，默认允许
        :param data: POST 表单字段；None 表示无表单数据
        :return: 供种子解析使用的响应；请求失败且没有响应时为 None
        """


_torrent_http_port: Optional[TorrentHttpPort] = None


def configure_torrent_port(http: TorrentHttpPort) -> None:
    """
    由组合根装配种子下载 HTTP 端口。
    """
    global _torrent_http_port
    _torrent_http_port = http


def reset_torrent_port() -> None:
    """
    清除种子下载 HTTP 端口。
    """
    global _torrent_http_port
    _torrent_http_port = None


def _require_torrent_port() -> TorrentHttpPort:
    """
    返回已装配端口，缺失时明确拒绝隐式构造 Adapter。
    """
    if _torrent_http_port is None:
        raise RuntimeError("种子下载端口尚未由启动组合根装配")
    return _torrent_http_port


# 站点首页、RSS 与音乐独立缓存的稳定键名；迁移脚本也复用这一事实来源。
_TORRENT_CACHE_KEYS = (
    "__torrents_cache__",
    "__rss_cache__",
    "__torrents_music_cache__",
    "__rss_music_cache__",
)


def clear_torrent_cache(cache_backend: Optional[Any] = None) -> None:
    """
    清理站点首页、RSS 及音乐资源缓存，不依赖 Chain 运行上下文。

    数据库迁移可能发生在生命周期组件装配之前，不能为了清理缓存构造
    ``TorrentsChain`` 并隐式拉起插件、模块和消息依赖，因此这里直接使用缓存端口。

    :param cache_backend: 可选缓存后端；未传入时使用当前宿主配置的文件缓存后端。
    """
    backend = cache_backend or FileCache()
    for cache_key in _TORRENT_CACHE_KEYS:
        backend.delete(cache_key)


@lru_cache(maxsize=512)
def _compile_filter_pattern(pattern: str) -> re.Pattern[str]:
    """
    编译订阅/工作流附加过滤正则。
    用户输入沿用原本的正则语义，缓存只减少同一规则反复匹配大量种子时的编译成本。
    """
    return re.compile(r"%s" % pattern, re.I)


def _filter_pattern_search(pattern: object, content: str) -> bool:
    """
    按原有字符串插值语义执行过滤正则匹配。
    """
    return bool(_compile_filter_pattern(str(pattern)).search(content))


@lru_cache(maxsize=256)
def _parse_filter_size_range(size_range: str) -> Tuple[str, float, Optional[float]]:
    """
    解析附加过滤的大小范围，单位为 MB。
    """
    if size_range.find("-") != -1:
        size_min, size_max = size_range.split("-")
        return "between", float(size_min.strip()) * _SIZE_UNIT, float(size_max.strip()) * _SIZE_UNIT
    if size_range.startswith(">"):
        return "gte", float(size_range[1:].strip()) * _SIZE_UNIT, None
    if size_range.startswith("<"):
        return "lte", 0, float(size_range[1:].strip()) * _SIZE_UNIT
    return "unknown", 0, None


def _cache_valid_torrent_content(
        parser: Callable[[Union[str, bytes]], Tuple[str, List[str]]],
        cache_backend: Any,
        cache_path: Path,
        content: bytes,
) -> Tuple[str, List[str]]:
    """
    解析合法种子并缓存原始二进制内容。
    """
    folder_name, file_list = parser(content)
    if file_list:
        cache_backend.set(cache_path.as_posix(), content, region="torrents")
    return folder_name, file_list


def _first_download_form(text: str, url: str) -> Optional[Tuple[str, dict[str, Any]]]:
    """
    解析下载确认表单，仅向当前下载端点提交有效控件，避免泄露站点凭据。
    """
    try:
        forms = html.fromstring(text).forms
    except (ValueError, html.etree.ParserError):
        return None
    for form in forms:
        action = form.get("action", "").strip()
        target = url if action in ("", "?") else urljoin(url, action)
        origin, destination = urlsplit(url), urlsplit(target)
        if (destination[:2] != origin[:2] or destination.path != origin.path
                or form.get("method", "post").lower() != "post"):
            continue
        data = dict(form.form_values())
        # form_values 不包含用户点击的提交控件，首次下载确认可能依赖该字段。
        submits = form.xpath(
            './/input[@name and not(@disabled) and translate(@type, "SUBMIT", "submit")="submit"]'
            ' | .//button[@name and not(@disabled) and (not(@type) or @type="submit")]'
        )
        if submits:
            data[submits[0].get("name")] = submits[0].get("value", "")
        if data:
            return target, data
    return None


def _text_filter_failure(torrent: TorrentInfo, params: Dict[str, Any], content: str) -> Optional[Tuple[str, str]]:
    """
    按原有顺序检查包含、排除、质量等正则，并返回业务原因和运行诊断。
    """
    for key, label, subject, expected in (
        ("include", "包含", content, True), ("exclude", "排除", content, False),
        ("quality", "质量", torrent.title, True), ("resolution", "分辨率", torrent.title, True),
        ("effect", "特效", torrent.title, True),
    ):
        pattern = params.get(key)
        if isinstance(pattern, str) and pattern and _filter_pattern_search(pattern, subject or "") != expected:
            diagnostic = f"{subject} {'不匹配' if expected else '匹配'}{label}规则 {pattern}"
            return f"不符合{label}条件：{pattern}", diagnostic
    return None


def _audio_minimum_failure(specs: Dict[str, Any], params: Dict[str, Any]) -> Optional[Tuple[str, Any]]:
    """
    检查音乐最低技术参数，不把缺失的参数当作达到要求。
    """
    for key, spec_key, label in (
        ("min_bitrate", "bitrate", "码率"), ("min_bit_depth", "bit_depth", "位深"),
        ("min_sample_rate", "sample_rate", "采样率"),
    ):
        minimum, actual = params.get(key), specs.get(spec_key)
        if minimum is not None and (actual is None or int(actual) < int(minimum)):
            return label, minimum
    return None


def _audio_filter_failure(torrent: TorrentInfo, params: Dict[str, Any], content: str) -> Optional[Tuple[str, str]]:
    """
    从标题、副标题和标签统一解析音质，再解释第一个未满足的音乐条件。
    """
    filters = {key: params.get(key) for key in ("audio_quality", "audio_format", "min_bitrate", "min_bit_depth", "min_sample_rate")
               if params.get(key) not in (None, "")}
    if not filters:
        return None
    specs = parse_audio_quality(content)
    tier = audio_quality_tier(**specs)
    quality = filters.get("audio_quality")
    pattern = "hires|lossless" if str(quality).casefold() == "lossless" else quality
    if quality and (not tier or not _filter_pattern_search(pattern, tier)):
        return f"不符合音乐音质条件：{quality}", f"{torrent.title} 不匹配音乐音质规则 {quality}"
    audio_format = filters.get("audio_format")
    normalized = normalize_audio_format(specs.get("audio_format"))
    if audio_format and (not normalized or not _filter_pattern_search(audio_format, normalized)):
        return f"不符合音频格式条件：{audio_format}", f"{torrent.title} 不匹配音频格式规则 {audio_format}"
    minimum = _audio_minimum_failure(specs, filters)
    if minimum:
        label, value = minimum
        return f"{label}不足，要求至少 {value}", f"{torrent.title} 不满足最低{label} {value}"
    return None


def _size_filter_failure(torrent: TorrentInfo, size_range: Optional[str]) -> Optional[str]:
    """
    解释大小范围判断，继续沿用原有 MB 单位和未知规则放行语义。
    """
    if not size_range:
        return None
    rule, minimum, maximum = _parse_filter_size_range(size_range)
    outside = (rule == "between" and maximum is not None and (torrent.size < minimum or torrent.size > maximum)) \
        or (rule == "gte" and torrent.size < minimum) or (rule == "lte" and maximum is not None and torrent.size > maximum)
    return f"资源大小不符合条件：{size_range}" if outside else None


def _title_fragments(value: str, separators: str) -> set[str]:
    """
    先归一化再排除英文碎片，避免 Dream! 等片段绕过单词过滤。
    """
    fragments = {text_tools.normalize_upper(part) for part in re.split(separators, value)} - {""}
    return {fragment for fragment in fragments if not text_tools.is_english_word(fragment)}


class TorrentHelper:
    """
    种子帮助类
    """

    def __init__(self) -> None:
        """
        初始化种子失败地址缓存
        """
        self._invalid_torrents = TTLCache(region="invalid_torrents", maxsize=128, ttl=3600 * 24)

    def _request_torrent_get(self, http_port: TorrentHttpPort, retry: bool,
                             **request_options: Any) -> Optional[TorrentResponsePort]:
        """
        普通种子 GET 瞬时失败时退避 1、2 秒；签名换票和确认 POST 不在此重试。
        """
        for attempt in range(3 if retry else 1):
            response = http_port.request(method="GET", **request_options)
            if not retry or attempt == 2 or not self._should_retry_torrent_response(response):
                return response
            logger.warning(f"种子下载遇到瞬时失败，将进行第 {attempt + 1} 次重试")
            time.sleep(attempt + 1)
        return None

    def _request_torrent_redirects(
        self,
        http_port: TorrentHttpPort,
        *,
        url: str,
        cookie: Optional[str],
        ua: Optional[str],
        referer: Optional[str],
        proxies: Optional[dict[str, Any]],
        retry: bool,
    ) -> tuple[Optional[TorrentResponsePort], str, Optional[str]]:
        """
        有界处理GET跳转，跨来源后永久移除原站点凭据和Referer。

        :param http_port: 已装配HTTP端口
        :param url: 当前下载地址
        :param cookie: 仅供原始来源使用的站点Cookie
        :param ua: 请求用户代理
        :param referer: 原始来源页地址
        :param proxies: 本次请求代理
        :param retry: 是否对普通GET执行既有瞬时失败重试
        :return: 最终响应、最终地址及允许在该地址使用的Cookie
        :raises ValueError: 跳转缺少地址、目标非法、重复或超过上限
        """
        seen = set()
        for _hop in range(9):
            if url in seen:
                raise ValueError("种子下载重定向出现环路")
            seen.add(url)
            response = self._request_torrent_get(
                http_port, retry and not torrent_rules.is_expiring_download_url(url),
                url=url, cookie=cookie, ua=ua, referer=referer,
                proxies=proxies, allow_redirects=False,
            )
            if response is None or response.status_code not in (301, 302, 303, 307, 308):
                return response, url, cookie
            location = response.headers.get("Location") or response.headers.get("location")
            close = getattr(response, "close", None)
            if callable(close):
                close()
            if not location:
                raise ValueError("种子下载重定向缺少目标地址")
            destination = urljoin(url, location)
            if destination.startswith("magnet:"):
                return None, destination, None
            target = urlsplit(destination)
            if target.scheme not in ("http", "https") or not target.hostname or target.username or target.password:
                raise ValueError("种子下载重定向目标无效")
            if not UrlUtils.same_origin(url, destination):
                cookie, referer = None, None
            url = destination
        raise ValueError("种子下载重定向次数超过上限")

    def _should_retry_torrent_response(self, response: Optional[TorrentResponsePort]) -> bool:
        """
        保留重定向、磁力和首次确认页，明确限流不得消耗瞬时重试预算。
        """
        if response is None:
            return True
        if _torrent_rate_limit_error(response):
            return False
        if response.status_code != 200:
            return _is_transient_torrent_status(response.status_code)
        content = response.content
        if content.startswith(b"magnet:"):
            return False
        if self.get_fileinfo_from_torrent_content(content)[1]:
            return False
        return "下载种子文件".encode("utf-8") not in content

    def download_torrent(self, url: str,
                         cookie: Optional[str] = None,
                         ua: Optional[str] = None,
                         referer: Optional[str] = None,
                         proxy: Optional[bool] = False,
                         cache_invalid: bool = True) \
            -> Tuple[Optional[Path], Optional[Union[str, bytes]], Optional[str], Optional[list[str]], Optional[str]]:
        """
        把种子下载到本地
        :param url: 种子下载地址
        :param cookie: 站点 Cookie
        :param ua: 请求 User-Agent
        :param referer: 请求来源地址
        :param proxy: 是否使用系统代理
        :param cache_invalid: 是否缓存失败地址并启用普通 GET 重试；短时凭证及换票地址必须关闭
        :return: 种子缓存相对路径【用于索引缓存】, 种子内容、种子主目录、种子文件清单、错误信息
        """
        if url.startswith("magnet:"):
            return None, url, "", [], "磁力链接"
        cache_path = Path(HashUtils.md5(url)).with_suffix(".torrent")
        original_url = url
        # 种子二进制固定落 TEMP_PATH/torrents 不进 Redis，下载器与提交链须按同一路由读取
        cache_backend = FileCache(local_only=True)
        torrent_content = cache_backend.get(cache_path.as_posix(), region="torrents")
        if torrent_content:
            try:
                folder_name, file_list = self.get_fileinfo_from_torrent_content(torrent_content)
                # 无法获取信息，则认为缓存文件无效
                if not folder_name and not file_list:
                    raise ValueError("无效的缓存种子文件")
                return cache_path, torrent_content, folder_name, file_list, ""
            except Exception as err:
                logger.error(f"处理缓存的种子文件 {cache_path} 时出错: {err}，将重新下载")
        config = get_chain_runtime_config_snapshot()
        http_port = _require_torrent_port()
        retry = cache_invalid and not torrent_rules.is_expiring_download_url(url)
        try:
            req, url, cookie = self._request_torrent_redirects(
                http_port, url=url, cookie=cookie, ua=ua, referer=referer,
                proxies=config.proxy if proxy else None, retry=retry,
            )
        except ValueError as err:
            return cache_path, None, "", [], str(err)
        if url.startswith("magnet:"):
            return None, url, "", [], "获取到磁力链接"
        if rate_limit_error := _torrent_rate_limit_error(req):
            return cache_path, None, "", [], rate_limit_error
        if req is not None and req.status_code == 200:
            if not req.content:
                return cache_path, None, "", [], "未下载到种子数据"
            if req.content.startswith(b"magnet:"):
                return cache_path, req.text, "", [], "获取到磁力链接"
            # 优先按原始二进制解析，避免合法 torrent 被误判为首次下载 HTML。
            folder_name, file_list = _cache_valid_torrent_content(
                self.get_fileinfo_from_torrent_content, cache_backend, cache_path, req.content
            )
            if file_list:
                return cache_path, req.content, folder_name, file_list, ""
            if "下载种子文件".encode("utf-8") in req.content:
                confirmation = _first_download_form(req.text, url)
                if confirmation is None:
                    logger.warning("触发首次种子下载，但未找到可安全提交的确认表单")
                    if cache_invalid:
                        self.add_invalid(original_url)
                    return cache_path, None, "", [], "种子数据有误，请确认链接是否正确，如为PT站点则需手工在站点下载一次种子"
                action, data = confirmation
                req = http_port.request(
                    method="POST", url=action, cookie=cookie, ua=ua,
                    referer=url, proxies=config.proxy if proxy else None, data=data, allow_redirects=False,
                )
                if rate_limit_error := _torrent_rate_limit_error(req):
                    return cache_path, None, "", [], rate_limit_error
                if req is None:
                    return cache_path, None, "", [], "无法打开链接"
                if req.status_code != 200:
                    return cache_path, None, "", [], f"下载种子出错，状态码：{req.status_code}"
                if not req.content:
                    return cache_path, None, "", [], "未下载到种子数据"
                logger.info("已提交站点首次种子下载确认，正在校验种子内容")
            # 首次下载提示页面完成后，重新按原始二进制解析 POST 响应。
            assert req is not None
            if req.content:
                folder_name, file_list = _cache_valid_torrent_content(
                    self.get_fileinfo_from_torrent_content, cache_backend, cache_path, req.content
                )
                if file_list:
                    return cache_path, req.content, folder_name, file_list, ""
            # 解析失败时终止，避免把 HTML 继续下沉到下载器或缓存。
            if cache_invalid:
                self.add_invalid(original_url)
            return cache_path, None, "", [], "种子数据有误，请确认链接是否正确"
        elif req is None:
            return cache_path, None, "", [], "无法打开链接"
        else:
            # 把错误的种子记下来，避免重复使用
            if cache_invalid and not _is_transient_torrent_status(req.status_code):
                self.add_invalid(original_url)
            return cache_path, None, "", [], f"下载种子出错，状态码：{req.status_code}"

    def get_torrent_info(self, torrent_path: Path) -> Tuple[str, List[str]]:
        """
        获取种子文件的文件夹名和文件清单
        :param torrent_path: 种子文件路径
        :return: 文件夹名、文件清单，单文件种子返回空文件夹名
        """
        if not torrent_path or not torrent_path.exists():
            return "", []
        try:
            torrentinfo = Torrent.from_file(torrent_path)
            return self.get_fileinfo_from_torrent(torrentinfo)
        except Exception as err:
            logger.error(f"种子文件解析失败：{str(err)}")
            return "", []

    @staticmethod
    def get_fileinfo_from_torrent(torrent: Torrent) -> Tuple[str, List[str]]:
        """
        从种子文件中获取文件清单
        :param torrent: 种子文件对象
        :return: 文件夹名、文件清单，单文件种子返回空文件夹名
        """
        if not torrent or not torrent.files:
            return "", []
        if len(torrent.files) == 1 and torrent.files[0].name == torrent.name:
            # 单文件种子目录名返回空
            folder_name = ""
            file_list = [torrent.name]
        else:
            folder_name = torrent.name
            # 文件清单，如果一级目录与种子名相同则去掉
            file_list = []
            for fileinfo in torrent.files:
                file_path = Path(fileinfo.name)
                root_path = file_path.parts[0]
                if root_path == folder_name:
                    file_list.append(str(file_path.relative_to(root_path)))
                else:
                    file_list.append(fileinfo.name)
        logger.debug(f"解析种子：{torrent.name} => 目录：{folder_name}，文件清单：{file_list}")
        return folder_name, file_list

    def get_fileinfo_from_torrent_content(self, torrent_content: Union[str, bytes]) -> Tuple[str, List[str]]:
        """
        从种子内容中获取文件夹名和文件清单
        :param torrent_content: 种子内容
        :return: 文件夹名、文件清单，单文件种子返回空文件夹名
        """

        if not torrent_content:
            return "", []

        if torrent_rules.is_magnet_link(torrent_content):
            return "", []

        try:
            torrentinfo = Torrent.from_string(torrent_content)
            return self.get_fileinfo_from_torrent(torrentinfo)
        except Exception as err:
            logger.error(f"种子内容解析失败：{str(err)}")
            return "", []

    @staticmethod
    def get_torrent_hash(content: Union[str, bytes]) -> Optional[str]:
        """
        从实际种子内容或磁力链接读取任务身份，不信任搜索卡片中的可能缺失 hash。
        """
        try:
            if torrent_rules.is_magnet_link(content):
                url = content.decode() if isinstance(content, bytes) else content
                values = parse_qs(urlsplit(url).query).get("xt", [])
                for value in values:
                    match = re.fullmatch(r"urn:btih:([a-fA-F0-9]{40})", value)
                    if match:
                        return match.group(1).lower()
                return None
            return str(Torrent.from_string(content).info_hash).lower()
        except (ValueError, TypeError, UnicodeError, KeyError, TorrentoolException):
            return None

    @staticmethod
    def get_url_filename(req: Any, url: str) -> str:
        """
        从下载请求中获取种子文件名。

        :param req: 下载响应；为空时返回空字符串
        :param url: 原始下载地址，用于响应头缺失时提取文件名
        :return: 响应头、地址或当前时间生成的文件名
        """
        if not req:
            return ""
        disposition = req.headers.get('content-disposition') or ""
        file_names = re.findall(r"filename=\"?(.+)\"?", disposition)
        if file_names:
            file_name = unquote(str(file_names[0].encode('ISO-8859-1').decode()).split(";")[0].strip())
            if file_name.endswith('"'):
                file_name = file_name[:-1]
        elif url and url.endswith(".torrent"):
            file_name = unquote(url.split("/")[-1])
        else:
            file_name = str(datetime.datetime.now())
        return file_name

    @staticmethod
    def sort_torrents(torrent_list: List[Context]) -> List[Context]:
        """
        对种子对行排序：torrent、site、upload、seeder
        """
        if not torrent_list:
            return []

        priority_rule: List[str] = get_configured_system_config().get(
            SystemConfigKey.TorrentsPriority) or ["torrent", "upload", "seeder"]
        site_uploads = {
            site.name: site.upload
            for site in get_configured_site_query_service().userdata_latest_sync()
        }

        def get_sort_str(_context: Context) -> str:
            """
            拼装排序字段
            """
            _meta = _context.meta_info or MetaInfo("")
            _torrent = _context.torrent_info or TorrentInfo()
            _media = _context.media_info
            _title = str(_media.title if _media else _meta.name).ljust(200, ' ')
            _site_order = str(999 - (_torrent.site_order or 0)).rjust(3, '0')
            _site_upload = str(site_uploads.get(_torrent.site_name) or 0).rjust(30, '0')
            _torrent_order = str(_torrent.pri_order or 0).rjust(3, '0')
            _torrent_seeders = str(_torrent.seeders or 0).rjust(10, '0')
            if not _meta.episode_list:
                # 无集数的排最前面
                _season_episode = "%s%s" % (str(len(_meta.season_list)).rjust(3, '0'), "9999")
            else:
                # 集数越多的排越前面
                _season_episode = "%s%s" % (str(len(_meta.season_list)).rjust(3, '0'),
                                            str(len(_meta.episode_list)).rjust(4, '0'))
            _sort_str = _title
            for rule in priority_rule:
                if rule == "torrent":
                    _sort_str += _torrent_order
                elif rule == "site":
                    _sort_str += _site_order
                elif rule == "upload":
                    _sort_str += _site_upload
                elif rule == "seeder":
                    _sort_str += _torrent_seeders
            _sort_str += _season_episode
            return _sort_str

        return sorted(torrent_list, key=lambda x: get_sort_str(x), reverse=True)

    def sort_group_torrents(self, torrent_list: List[Context]) -> List[Context]:
        """
        对媒体信息进行排序、去重
        """
        if not torrent_list:
            return []

        torrent_list = self.sort_torrents(torrent_list)

        result = []
        _added = []
        # 排序后重新加入数组，按真实名称控重，即只取每个名称的第一个
        for context in torrent_list:
            # 控重的主链是名称、年份、季、集
            meta = context.meta_info
            media = context.media_info
            if meta is None or media is None:
                continue
            if media.type == MediaType.TV:
                media_name = "%s%s" % (media.title_year,
                                       meta.season_episode)
            else:
                media_name = media.title_year
            if media_name not in _added:
                _added.append(media_name)
                result.append(context)

        return result

    @staticmethod
    def get_torrent_episodes(files: list[str], custom_words: Optional[List[str]] = None) -> list[int]:
        """
        从种子的文件清单中获取所有集数

        :param files: 种子文件清单
        :param custom_words: 当前下载来源的临时自定义识别词
        :return: 识别到的全部集数
        """
        episodes: list[int] = []
        config = get_chain_runtime_config_snapshot()
        for file in files:
            if not file:
                continue
            file_path = Path(file)
            if not file_path.suffix or file_path.suffix.lower() not in config.video_extensions:
                continue
            # 只使用文件名识别
            meta = MetaInfo(file_path.name, custom_words=custom_words)
            if not meta.begin_episode:
                continue
            episodes = list(set(episodes).union(set(meta.episode_list)))
        return episodes

    def is_invalid(self, url: Optional[str]) -> bool:
        """
        判断种子是否是无效种子
        """
        return url in self._invalid_torrents if url else True

    def add_invalid(self, url: str) -> None:
        """
        缓存无效种子地址。

        :param url: 需要在后续请求中跳过的种子地址
        """
        if url not in self._invalid_torrents:
            self._invalid_torrents[url] = True

    @staticmethod
    def match_torrent(mediainfo: MediaInfo, torrent_meta: MetaBase, torrent: TorrentInfo) -> bool:
        """
        检查种子是否匹配媒体信息

        完整名称可匹配英文标题或别名；拆分片段归一化后排除英文单词和空值，
        防止标点把普通单词变成作品身份依据。
        :param mediainfo: 需要匹配的媒体信息
        :param torrent_meta: 种子识别信息
        :param torrent: 种子信息
        """
        # 显式标题标签与识别结果使用同一主身份比较。
        torrent_identity = resolve_media_identity(media=torrent_meta)
        media_identity = resolve_media_identity(media=mediainfo)
        if all(torrent_identity) and torrent_identity == media_identity:
            logger.info(
                f'{mediainfo.title} 通过词表指定媒体身份 '
                f'{torrent_identity[0]}:{torrent_identity[1]} 匹配到资源：'
                f'{torrent.site_name} - {torrent.title}'
            )
            return True
        media_titles = {
            text_tools.normalize_upper(mediainfo.title),
            text_tools.normalize_upper(mediainfo.original_title)
        } - {""}
        media_names = {text_tools.normalize_upper(name) for name in mediainfo.names if name} - {""}
        meta_names = {
            text_tools.normalize_upper(torrent_meta.cn_name),
            text_tools.normalize_upper(torrent_meta.en_name)
        } - {""}
        media_type = mediainfo.type or MediaType.UNKNOWN
        if torrent_meta.type == MediaType.TV and media_type != MediaType.TV:
            logger.debug(f'{torrent.site_name} - {torrent.title} 种子标题类型为 {torrent_meta.type.value}，'
                         f'不匹配 {media_type.value}')
            return False
        if torrent.category == MediaType.TV.value and media_type != MediaType.TV:
            logger.debug(f'{torrent.site_name} - {torrent.title} 种子在站点中归类为 {torrent.category}，'
                         f'不匹配 {media_type.value}')
            return False
        if mediainfo.year:
            if mediainfo.type == MediaType.TV:
                # 剧集年份，每季的年份可能不同，没年份时不比较年份（很多剧集种子不带年份）
                if torrent_meta.year and torrent_meta.year not in [year for year in
                                                                   mediainfo.season_years.values()]:
                    logger.debug(f'{torrent.site_name} - {torrent.title} 年份不匹配 {mediainfo.season_years}')
                    return False
            else:
                # 电影年份，上下浮动1年，没年份时不通过
                if not torrent_meta.year or torrent_meta.year not in [str(int(mediainfo.year) - 1),
                                                                      mediainfo.year,
                                                                      str(int(mediainfo.year) + 1)]:
                    logger.debug(f'{torrent.site_name} - {torrent.title} 年份不匹配 {mediainfo.year}')
                    return False
        # 无年份同名剧仅靠标题无法消歧，先用目标剧已知的季集拓扑排除不可能候选。
        if mediainfo.type == MediaType.TV and not TorrentHelper._match_tv_topology(
                mediainfo=mediainfo,
                torrent_meta=torrent_meta,
                torrent=torrent,
        ):
            return False
        if meta_names.intersection(media_titles):
            logger.info(f'{mediainfo.title} 通过标题匹配到资源：{torrent.site_name} - {torrent.title}')
            return True
        if media_names:
            if meta_names.intersection(media_names):
                logger.info(f'{mediainfo.title} 通过别名或译名匹配到资源：{torrent.site_name} - {torrent.title}')
                return True
        if torrent_meta.org_string:
            # 完整英文名称已在上方比较，拆分后的英文单词不能单独证明作品身份。
            titles = _title_fragments(torrent_meta.org_string, r'[\s/【】.\[\]\-]+')
            # 在标题中判断是否存在标题、原语种标题
            if media_titles.intersection(titles):
                logger.info(f'{mediainfo.title} 通过标题匹配到资源：{torrent.site_name} - {torrent.title}')
                return True
        # 在副标题中（非英文单词）判断是否存在标题、原语种标题、别名、译名
        if torrent.description:
            subtitles = _title_fragments(torrent.description, r'[\s/【】|]+')
            if media_titles.intersection(subtitles) or media_names.intersection(subtitles):
                logger.info(f'{mediainfo.title} 通过副标题匹配到资源：{torrent.site_name} - {torrent.title}，'
                            f'副标题：{torrent.description}')
                return True
        logger.debug(f'{torrent.site_name} - {torrent.title} 标题不匹配，识别名称：{meta_names}')
        return False

    @staticmethod
    def _match_tv_topology(
            mediainfo: MediaInfo,
            torrent_meta: MetaBase,
            torrent: TorrentInfo,
    ) -> bool:
        """
        用目标剧已知的季集范围排除无年份同名候选。
        """
        begin_season = getattr(torrent_meta, "begin_season", None)
        end_season = getattr(torrent_meta, "end_season", None)
        if begin_season is None:
            torrent_seasons = set()
        elif end_season is None:
            torrent_seasons = {begin_season}
        else:
            start, end = sorted((begin_season, end_season))
            torrent_seasons = set(range(start, end + 1))

        known_seasons = set()
        for season_map in (
                getattr(mediainfo, "seasons", None),
                getattr(mediainfo, "season_years", None),
        ):
            for season in (season_map or {}).keys():
                try:
                    known_seasons.add(int(season))
                except (TypeError, ValueError):
                    continue
        if not known_seasons:
            try:
                season_count = int(getattr(mediainfo, "number_of_seasons", None) or 0)
            except (TypeError, ValueError):
                season_count = 0
            if season_count:
                known_seasons = set(range(1, season_count + 1))

        if torrent_seasons and known_seasons and not torrent_seasons.issubset(known_seasons):
            logger.debug(
                f'{torrent.site_name} - {torrent.title} 季范围 {sorted(torrent_seasons)} '
                f'超出目标媒体季范围 {sorted(known_seasons)}'
            )
            return False

        if len(torrent_seasons) != 1:
            return True
        season = next(iter(torrent_seasons))
        target_episodes = (getattr(mediainfo, "seasons", None) or {}).get(season) or []
        if not target_episodes and len(known_seasons) == 1:
            try:
                episode_count = int(getattr(mediainfo, "number_of_episodes", None) or 0)
            except (TypeError, ValueError):
                episode_count = 0
            if episode_count:
                target_episodes = list(range(1, episode_count + 1))
        torrent_episodes = getattr(torrent_meta, "episode_list", None) or []
        if target_episodes and torrent_episodes and max(torrent_episodes) > max(target_episodes):
            logger.debug(
                f'{torrent.site_name} - {torrent.title} 最大集数 {max(torrent_episodes)} '
                f'超出目标媒体第 {season} 季总集数 {max(target_episodes)}'
            )
            return False
        return True

    @staticmethod
    def requires_identity_disambiguation(
            mediainfo: MediaInfo,
            torrent_meta: MetaBase,
    ) -> bool:
        """
        判断无年份标题是否只通过目标别名命中，需进一步核验媒体身份。
        """
        if (
                mediainfo.type != MediaType.TV
                or getattr(torrent_meta, "year", None)
                or not getattr(mediainfo, "year", None)
        ):
            return False
        media_titles = {
            text_tools.normalize_upper(value)
            for value in (
                getattr(mediainfo, "title", None),
                getattr(mediainfo, "original_title", None),
            )
            if value
        }
        media_names = {
            text_tools.normalize_upper(value)
            for value in (getattr(mediainfo, "names", None) or [])
            if value
        }
        meta_names = {
            text_tools.normalize_upper(value)
            for value in (
                getattr(torrent_meta, "cn_name", None),
                getattr(torrent_meta, "en_name", None),
            )
            if value
        }
        return bool(meta_names.intersection(media_names)) and not bool(
            meta_names.intersection(media_titles)
        )

    @staticmethod
    def match_same_work_evidence(
            target_mediainfo: MediaInfo,
            candidate_mediainfo: MediaInfo,
            torrent_meta: MetaBase,
    ) -> Tuple[bool, str]:
        """
        判断媒体 ID 冲突时是否存在足以覆盖候选识别的同作品证据。

        TMDB 的电影与电视剧数字 ID 不可跨类型比较；类型冲突时仍需资源精确年份、
        其他共同标识或一致的作品元数据，不能仅凭相同 TMDB ID 覆盖候选。

        :return: 是否有同作品证据，以及用于诊断的证据或拒绝原因
        """
        target_identity = resolve_media_identity(media=target_mediainfo)
        candidate_identity = resolve_media_identity(media=candidate_mediainfo)
        same_type = target_mediainfo.type == candidate_mediainfo.type
        if all(target_identity) and target_identity == candidate_identity and (
                same_type or target_identity[0] != MediaSource.TMDB
        ):
            return True, f"媒体身份 {target_identity[0]}:{target_identity[1]}"

        torrent_year = str(getattr(torrent_meta, "year", None) or "")[:4]
        target_years = {
            str(year)[:4]
            for year in (
                [getattr(target_mediainfo, "year", None)]
                + list((getattr(target_mediainfo, "season_years", None) or {}).values())
            )
            if year
        }
        if torrent_year and torrent_year in target_years:
            return True, f"资源年份 {torrent_year}"

        for field in ("tmdb_id", "imdb_id", "tvdb_id", "douban_id", "bangumi_id"):
            if field == "tmdb_id" and not same_type:
                continue
            target_id = getattr(target_mediainfo, field, None)
            candidate_id = getattr(candidate_mediainfo, field, None)
            if target_id and candidate_id and str(target_id) == str(candidate_id):
                return True, f"共同 {field}"

        target_year = str(getattr(target_mediainfo, "year", None) or "")[:4]
        candidate_year = str(getattr(candidate_mediainfo, "year", None) or "")[:4]
        if target_year and candidate_year and target_year != candidate_year:
            return False, f"首播年份冲突 {candidate_year} != {target_year}"

        target_original_title = text_tools.normalize_upper(
            getattr(target_mediainfo, "original_title", None) or ""
        )
        candidate_original_title = text_tools.normalize_upper(
            getattr(candidate_mediainfo, "original_title", None) or ""
        )
        if (
                target_original_title
                and candidate_original_title
                and target_original_title != candidate_original_title
        ):
            return False, "原始标题冲突"

        target_language = str(
            getattr(target_mediainfo, "original_language", None) or ""
        ).casefold()
        candidate_language = str(
            getattr(candidate_mediainfo, "original_language", None) or ""
        ).casefold()
        if target_language and candidate_language and target_language != candidate_language:
            return False, f"原始语言冲突 {candidate_language} != {target_language}"

        if target_year and candidate_year:
            return True, f"共同首播年份 {target_year}"
        if target_original_title and candidate_original_title:
            return True, "共同原始标题"
        return False, "资源无年份，候选与目标也没有可核验的共同元数据"

    @staticmethod
    def filter_torrent(torrent_info: TorrentInfo,
                       filter_params: Dict[str, Any]) -> bool:
        """
        检查种子是否匹配订阅条件，保留既有布尔 API 和运行日志行为。
        """
        return TorrentHelper.filter_torrent_reason(torrent_info, filter_params, log_failure=True) is None

    @staticmethod
    def filter_torrent_reason(torrent_info: TorrentInfo,
                              filter_params: Dict[str, Any], *, log_failure: bool = False) -> Optional[str]:
        """
        返回首个未满足的订阅条件；布尔筛选复用同一判断，活动不重复写运行日志。
        """

        if not filter_params:
            return None
        content = (f"{torrent_info.title} {torrent_info.description} "
                   f"{' '.join(torrent_info.labels or [])} {torrent_info.volume_factor}")
        failure = _text_filter_failure(torrent_info, filter_params, content)
        if failure is None:
            failure = _audio_filter_failure(torrent_info, filter_params, content)
        if failure is not None:
            reason, diagnostic = failure
            if log_failure:
                logger.info(diagnostic)
            return reason
        return _size_filter_failure(torrent_info, filter_params.get("size"))

    @staticmethod
    def match_season_episodes(torrent: TorrentInfo, meta: MetaBase, season_episodes: Dict[int, list[int]]) -> bool:
        """
        判断种子是否匹配季集数
        :param torrent: 种子信息
        :param meta: 种子元数据
        :param season_episodes: 季集数 {season:[episodes]}
        """
        seasons = season_episodes.keys()
        seasons_set = set(seasons)
        torrent_seasons = meta.season_list
        if not torrent_seasons:
            # 按第一季处理
            torrent_seasons = [1]
        torrent_episodes = meta.episode_list
        if not set(torrent_seasons).issubset(seasons_set):
            logger.debug(
                f"种子 {torrent.site_name} - {torrent.title} 包含季 {torrent_seasons} 不是需要的季 {list(seasons)}")
            return False
        if not torrent_episodes:
            # 整季按匹配处理
            return True
        if len(torrent_seasons) == 1:
            need_episodes = season_episodes.get(torrent_seasons[0])
            if need_episodes \
                    and not set(torrent_episodes).intersection(need_episodes):
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} "
                             f"集 {torrent_episodes} 没有需要的集：{need_episodes}")
                return False
        return True
