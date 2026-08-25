from app.adapters.system import rust as rust_accelerator
from app.domain.context import configure_tmdb_image_url_builder
from app.domain.media import configure_search_source_provider
from app.domain.meta.customization import configure_customization_provider
from app.domain.meta.releasegroup import configure_release_groups_provider
from app.domain.meta.runtime import configure_recognition_runtime
from app.domain.meta.words import configure_custom_words_provider
from app.domain.metainfo import clear_rust_parse_options_cache
from app.runtime.settings import RuntimeSettingsCompat

settings = RuntimeSettingsCompat()
from app.application.recognition import RecognitionRuleService


def configure_domain_dependencies() -> None:
    """在组合根集中注入领域模型需要的配置、持久化规则和加速适配器。"""
    rule_service = RecognitionRuleService()
    configure_customization_provider(rule_service.get_customization)
    configure_release_groups_provider(rule_service.get_release_groups)
    configure_custom_words_provider(rule_service.get_custom_words)
    configure_search_source_provider(lambda: settings.SEARCH_SOURCE)
    configure_tmdb_image_url_builder(settings.TMDB_IMAGE_URL)
    configure_recognition_runtime(
        media_extensions_provider=lambda: (
            *settings.RMT_MEDIAEXT,
            *settings.RMT_SUBEXT,
            *settings.RMT_AUDIOEXT,
        ),
        audio_extensions_provider=lambda: settings.RMT_AUDIOEXT,
        accelerator=rust_accelerator,
    )
    clear_rust_parse_options_cache()
