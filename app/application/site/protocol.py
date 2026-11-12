"""
站点协议能力的应用端口，不发现或直接导入具体站点模块。

启动组合根把端口接到宿主模块调度器；领域规则只处理通用事实，不反向调用此端口。
"""

from dataclasses import dataclass
from typing import Mapping, Optional, Protocol

from app.domain.context import TorrentInfo


@dataclass(frozen=True, slots=True)
class SiteConnectionResult:
    """
    Module 的连接检测回执，区分不支持、真实失败和模块根本没有返回结果。

    :param supported: 是否由适配器接管检测；False 时用例才可执行通用网页检测
    :param success: 适配器检测是否成功；不支持时保持 False
    :param message: 可以展示给用户的连接、认证或权限说明
    """

    supported: bool
    success: bool = False
    message: str = ""


class SiteProtocolPort(Protocol):
    """
    声明公共用例所需的站点协议解释能力。

    实现方通过模块分发选择站点适配器；这些方法不持有数据库会话或执行网络请求。
    """

    def get_site_domain(self, url: Optional[str]) -> str:
        """
        解析站点匹配域名，保留适配器声明的多级域名。

        :param url: 站点地址或 Cookie Domain；空值返回空字符串
        :return: 用于站点目录、持久化和流控匹配的域名
        """
        ...

    def torrent_resource_identity(self, torrent: TorrentInfo) -> Optional[tuple[str, str]]:
        """
        结合通用身份字段和适配器的 URL 规则解释资源身份。

        :param torrent: 当前候选或旧缓存中的种子信息，不修改输入
        :return: 站点和稳定资源标识；不能明确定位时为 None
        """
        ...

    def is_expiring_download_url(self, url: Optional[str]) -> bool:
        """
        判断下载地址是否携带适配器声明的短时票据。

        :param url: 原始或重定向后的下载地址
        :return: 已知短时票据为 True；普通、磁力及间接配方为 False
        """
        ...

    def is_torrent_download_confirmation(self, content: bytes) -> bool:
        """
        识别需要用户首次下载确认的站点响应。

        :param content: 尚未成功解析为种子的响应原始字节
        :return: 命中已支持的下载确认协议时为 True
        """
        ...

    def parse_torrent_download_confirmation(self, text: str, url: str) -> Optional[tuple[str, dict[str, object]]]:
        """
        解释确认表单，仅允许向原下载端点提交。

        :param text: 站点下载确认页的 HTML 文本
        :param url: 确认页的最终请求地址，作为来源和安全边界
        :return: 安全的提交地址与控件；没有合法表单时为 None
        """
        ...

    def has_site_subtitle_api(self, site: Mapping[str, object]) -> bool:
        """
        判断站点字幕是否由独立协议处理，避免普通模块再探测网页。

        :param site: 站点资源声明
        :return: 适配器提供独立字幕接口时为 True
        """
        ...

    def encrypt_site_payload(self, data_str: str, key: bytes) -> str:
        """
        通过 Module 生成旧插件使用的站点协议加密载荷。

        :param data_str: 待加密的站点文本
        :param key: 协议约定的密钥字节
        :return: 站点协议包装的密文
        """
        ...


_site_protocol_port: Optional[SiteProtocolPort] = None


def configure_site_protocol_port(port: SiteProtocolPort) -> None:
    """
    由启动组合根发布通过模块调度运行的站点协议端口。

    :param port: 不物化站点或网络资源的协议适配端口
    """
    global _site_protocol_port
    _site_protocol_port = port


def reset_site_protocol_port() -> None:
    """
    清除协议端口，避免关闭后继续使用旧运行时。
    """
    global _site_protocol_port
    _site_protocol_port = None


def get_site_protocol_port() -> SiteProtocolPort:
    """
    返回已装配端口，未装配时不隐式创建模块或 Chain。

    :return: 当前生命周期的站点协议端口
    :raises RuntimeError: 启动组合根尚未装配端口
    """
    if _site_protocol_port is None:
        raise RuntimeError("站点协议端口尚未由启动组合根装配")
    return _site_protocol_port


def site_domain(url: Optional[str]) -> str:
    """
    通过站点 Module 解析公共用例需要的匹配域名。

    :param url: 站点地址或 Cookie Domain
    :return: Module 解释的域名；空地址为空字符串
    """
    return get_site_protocol_port().get_site_domain(url)


def torrent_resource_identity(torrent: TorrentInfo) -> Optional[tuple[str, str]]:
    """
    通过站点 Module 解释旧缓存、搜索和下载共用的资源身份。

    :param torrent: 不修改的种子候选
    :return: 站点及稳定资源标识；无法确定时为 None
    """
    return get_site_protocol_port().torrent_resource_identity(torrent)


def is_expiring_download_url(url: Optional[str]) -> bool:
    """
    通过站点 Module 判断原始或重定向下载地址的票据时效性。

    :param url: 待判断的下载地址
    :return: 是否属于已支持的短时票据协议
    """
    return get_site_protocol_port().is_expiring_download_url(url)


def nexusphp_encrypt(data_str: str, key: bytes) -> str:
    """
    保留插件加密入口的参数和结果，协议包装由站点 Module 实现。

    :param data_str: 待加密的站点文本
    :param key: 协议约定的密钥字节
    :return: 与旧插件接口一致的加密载荷
    """
    return get_site_protocol_port().encrypt_site_payload(data_str, key)
