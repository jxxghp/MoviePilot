"""Chain mixin Host Protocol 与依赖方向门禁。"""

import ast
from pathlib import Path

from app.chain._contracts import (
    ChainRuntimeMixinHost,
    InteractionMixinHost,
    MusicSubscribeMixinHost,
    TransferMixinHost,
)
from app.chain._interaction import InteractionChainMixin
from app.chain._messaging import MessageProcessingMixin, NotificationMixin
from app.chain._music import MusicSubscribeMixin
from app.chain._recognition import RecognitionMixin
from app.chain._transfer import (
    EpisodeFormatMixin,
    FailedRetryMixin,
    FileFilterMixin,
    FileKeyMixin,
    HistoryMatchMixin,
    ManualHistoryMixin,
    ScrapeBatchMixin,
)
from app.chain.subscribe import SubscribeChain
from app.chain.transfer import TransferChain


def test_all_chain_mixins_declare_their_host_protocol() -> None:
    """每个存量 Chain mixin 都必须显式声明宿主能力契约。"""
    expected = {
        RecognitionMixin: ChainRuntimeMixinHost,
        MessageProcessingMixin: ChainRuntimeMixinHost,
        NotificationMixin: ChainRuntimeMixinHost,
        InteractionChainMixin: InteractionMixinHost,
        MusicSubscribeMixin: MusicSubscribeMixinHost,
        FileFilterMixin: TransferMixinHost,
        ScrapeBatchMixin: TransferMixinHost,
        EpisodeFormatMixin: TransferMixinHost,
        HistoryMatchMixin: TransferMixinHost,
        FileKeyMixin: TransferMixinHost,
        ManualHistoryMixin: TransferMixinHost,
        FailedRetryMixin: TransferMixinHost,
    }

    assert {
        mixin: mixin.__mixin_host_protocol__ for mixin in expected
    } == expected


def test_mixin_hosts_provide_injected_chain_factories() -> None:
    """具体 Chain 必须提供 mixin 契约要求的链工厂接缝。"""
    for name in (
        "_music_media_chain",
        "_music_download_chain",
        "_music_search_chain",
        "_music_site_keywords",
        "_matches_music_resource",
    ):
        assert callable(getattr(SubscribeChain, name))
    for name in (
        "_transfer_media_chain",
        "_transfer_storage_chain",
        "_transfer_subscribe_chain",
    ):
        assert callable(getattr(TransferChain, name))


def test_domain_mixins_keep_concrete_imports_explicit_until_next_migration() -> None:
    """第一阶段先锁定契约标记，具体导入在后续批次逐步下沉到宿主工厂。"""
    root = Path(__file__).resolve().parents[1]
    violations = []
    for relative in ("app/chain/_music.py", "app/chain/_transfer.py"):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("app.chain.") and node.module != "app.chain._contracts":
                    violations.append(f"{relative}:{node.module}")

    assert set(violations) <= {
        "app/chain/_music.py:app.chain.download",
        "app/chain/_music.py:app.chain.media",
        "app/chain/_music.py:app.chain.search",
        "app/chain/_transfer.py:app.chain.media",
        "app/chain/_transfer.py:app.chain.storage",
        "app/chain/_transfer.py:app.chain.subscribe",
    }
