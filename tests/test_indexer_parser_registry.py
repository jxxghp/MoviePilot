"""站点解析器注册表：按 schema 标识登记与解析的守护测试。"""

from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.modules.indexer.parser.bitpt import BitptSiteUserInfo
from app.modules.indexer.parser.discuz import DiscuzUserInfo
from app.modules.indexer.parser.file_list import FileListSiteUserInfo
from app.modules.indexer.parser.gazelle import GazelleSiteUserInfo
from app.modules.indexer.parser.hddolby import HDDolbySiteUserInfo
from app.modules.indexer.parser.ipt_project import IptSiteUserInfo
from app.modules.indexer.parser.mtorrent import MTorrentSiteUserInfo
from app.modules.indexer.parser.nexus_audiences import NexusAudiencesSiteUserInfo
from app.modules.indexer.parser.nexus_hhanclub import NexusHhanclubSiteUserInfo
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo
from app.modules.indexer.parser.nexus_project import NexusProjectSiteUserInfo
from app.modules.indexer.parser.nexus_rabbit import NexusRabbitSiteUserInfo
from app.modules.indexer.parser.registry import (
    load_builtin_parsers,
    registered_schemas,
    resolve_parser_class,
    site_parser_identity,
)
from app.modules.indexer.parser.rousi import RousiSiteUserInfo
from app.modules.indexer.parser.small_horse import SmallHorseSiteUserInfo
from app.modules.indexer.parser.sunnypt import SunnyPTSiteUserInfo
from app.modules.indexer.parser.tnode import TNodeSiteUserInfo
from app.modules.indexer.parser.torrent_leech import TorrentLeechSiteUserInfo
from app.modules.indexer.parser.unit3d import Unit3dSiteUserInfo
from app.modules.indexer.parser.yema import YemaSiteUserInfo
from app.modules.indexer.parser.zhixing import ZhixingSiteUserInfo

# 内建站点解析器的标识与承载类，覆盖 SiteSchema 全部 20 个成员
BUILTIN_PARSERS = {
    "Bitpt": BitptSiteUserInfo,
    "DiscuzX": DiscuzUserInfo,
    "FileList": FileListSiteUserInfo,
    "Gazelle": GazelleSiteUserInfo,
    "HDDolby": HDDolbySiteUserInfo,
    "IPTorrents": IptSiteUserInfo,
    "MTorrent": MTorrentSiteUserInfo,
    "NexusAudiences": NexusAudiencesSiteUserInfo,
    "NexusHhanclub": NexusHhanclubSiteUserInfo,
    "NexusPhp": NexusPhpSiteUserInfo,
    "NexusProject": NexusProjectSiteUserInfo,
    "NexusRabbit": NexusRabbitSiteUserInfo,
    "RousiPro": RousiSiteUserInfo,
    "Small Horse": SmallHorseSiteUserInfo,
    "SunnyPT": SunnyPTSiteUserInfo,
    "TNode": TNodeSiteUserInfo,
    "TorrentLeech": TorrentLeechSiteUserInfo,
    "Unit3d": Unit3dSiteUserInfo,
    "Yema": YemaSiteUserInfo,
    "Zhixing": ZhixingSiteUserInfo,
}


class _MinimalParser(SiteParserBase):
    """实现全部抽象方法的最小站点解析器，仅用于验证登记机制。"""

    def _parse_message_unread_links(self, html_text, msg_links):
        """不解析任何未读消息链接。"""
        return None

    def _parse_site_page(self, html_text):
        """不解析站点页面。"""
        pass

    def _parse_user_base_info(self, html_text):
        """不解析用户基础信息。"""
        pass

    def _parse_user_traffic_info(self, html_text):
        """不解析用户流量信息。"""
        pass

    def _parse_user_torrent_seeding_info(self, html_text, multi_page=False):
        """不解析用户做种信息，且没有下一页。"""
        return None

    def _parse_user_detail_info(self, html_text):
        """不解析用户详细信息。"""
        pass

    def _parse_message_content(self, html_text):
        """不解析消息内容。"""
        return None, None, None


def test_every_builtin_parser_resolves_by_its_declared_schema():
    """
    每个内建解析器都应能按其声明的 schema 标识查得，覆盖 SiteSchema 全部成员。
    """
    assert set(BUILTIN_PARSERS) == {member.value for member in SiteSchema}
    for schema, parser_cls in BUILTIN_PARSERS.items():
        assert resolve_parser_class(schema) is parser_cls


def test_nexus_php_base_and_subclasses_resolve_to_distinct_classes():
    """
    NexusPhp 及其派生的 hhanclub/project/audiences 解析器互不覆盖对方的登记。
    """
    assert resolve_parser_class("NexusPhp") is NexusPhpSiteUserInfo
    assert resolve_parser_class("NexusHhanclub") is NexusHhanclubSiteUserInfo
    assert resolve_parser_class("NexusProject") is NexusProjectSiteUserInfo
    assert resolve_parser_class("NexusAudiences") is NexusAudiencesSiteUserInfo
    assert NexusHhanclubSiteUserInfo is not NexusPhpSiteUserInfo
    assert issubclass(NexusHhanclubSiteUserInfo, NexusPhpSiteUserInfo)


def test_unknown_schema_resolves_to_none():
    """
    未登记的 schema 标识以及缺省值都应解析为 None。
    """
    assert resolve_parser_class("some-unregistered-schema") is None
    assert resolve_parser_class(None) is None
    assert resolve_parser_class("") is None


def test_abstract_base_is_not_registered():
    """
    schema 为 None 的抽象基类不应出现在注册表中。
    """
    assert site_parser_identity(SiteParserBase) is None
    assert SiteParserBase not in {
        resolve_parser_class(schema) for schema in registered_schemas()
    }


def test_site_parser_identity_reads_enum_and_free_string_alike():
    """
    站点标识既接受 SiteSchema 枚举成员，也接受自由字符串。
    """
    assert site_parser_identity(NexusPhpSiteUserInfo) == "NexusPhp"

    class _FreeIdentityParser(SiteParserBase):
        schema = "free-string-schema"

    assert site_parser_identity(_FreeIdentityParser) == "free-string-schema"
    assert site_parser_identity(object()) is None


def test_new_parser_class_registers_without_touching_registry_or_enum():
    """
    新增解析器只需实现类并声明 schema 标识即完成登记，无需改动注册表或 SiteSchema 枚举。

    "unregistered-brand-new-site" 从未出现在 SiteSchema 枚举或 registry.py 中；
    仅通过在此定义 SiteParserBase 子类并声明该标识，即可被 resolve_parser_class 解析到。
    """
    assert "unregistered-brand-new-site" not in {member.value for member in SiteSchema}
    assert resolve_parser_class("unregistered-brand-new-site") is None

    class _BrandNewSiteParser(_MinimalParser):
        schema = "unregistered-brand-new-site"

    assert resolve_parser_class("unregistered-brand-new-site") is _BrandNewSiteParser
    assert "unregistered-brand-new-site" in registered_schemas()


def test_load_builtin_parsers_is_idempotent():
    """
    重复导入内建解析器包不改变已登记的标识集合。
    """
    before = set(registered_schemas())

    load_builtin_parsers()

    assert set(registered_schemas()) == before
