from app.adapters.network.resolver import SocketDnsResolver
from app.adapters.system import rust as rust_accelerator
from app.adapters.system.host import SystemUtils
from app.application.directory import configure_disk_topology
from app.application.recognition import RecognitionRuleService
from app.application.rules import configure_filter_rule_parser
from app.application.security.url import configure_dns_resolver
from app.application.transfer.workflow import configure_directory_size
from app.domain.media import configure_search_source_provider
from app.domain.meta.customization import configure_customization_provider
from app.domain.meta.releasegroup import configure_release_groups_provider
from app.domain.meta.runtime import configure_recognition_runtime
from app.domain.meta.words import configure_custom_words_provider
from app.domain.metainfo import clear_rust_parse_options_cache
from app.domain.projection.tmdb import (
    configure_image_url_builder as configure_tmdb_image_url_builder,
)
from app.runtime.settings import get_runtime_setting


def configure_domain_dependencies() -> None:
    """在组合根集中注入领域模型需要的配置、持久化规则和加速适配器。"""
    rule_service = RecognitionRuleService()
    configure_dns_resolver(SocketDnsResolver())
    configure_disk_topology(SystemUtils)
    configure_filter_rule_parser(rust_accelerator)
    configure_directory_size(SystemUtils)
    configure_customization_provider(rule_service.get_customization)
    configure_release_groups_provider(rule_service.get_release_groups)
    configure_custom_words_provider(rule_service.get_custom_words)
    configure_search_source_provider(lambda: get_runtime_setting('SEARCH_SOURCE'))
    configure_tmdb_image_url_builder(get_runtime_setting('TMDB_IMAGE_URL'))
    configure_recognition_runtime(
        media_extensions_provider=lambda: (
            *get_runtime_setting('RMT_MEDIAEXT'),
            *get_runtime_setting('RMT_SUBEXT'),
            *get_runtime_setting('RMT_AUDIOEXT'),
        ),
        audio_extensions_provider=lambda: get_runtime_setting('RMT_AUDIOEXT'),
        accelerator=rust_accelerator,
    )
    clear_rust_parse_options_cache()
