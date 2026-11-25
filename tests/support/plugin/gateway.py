"""
插件安装网关的市场来源与库存回放；不注册测试用例。
"""

from datetime import datetime, timezone
from app.application.plugin.identity import TrustedPluginSourceType
from app.application.plugin.source import CandidateInventory, MarketRead, PluginMarketCandidate


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


REPO_URL = "https://github.com/jxxghp/MoviePilot-Plugins"


def _inventory(plugin_version: str = "1.0.0") -> CandidateInventory:
    """
    构造仅含官方候选的完整库存。
    """
    return CandidateInventory((
        MarketRead.present(
            REPO_URL,
            (
                PluginMarketCandidate(
                    plugin_id="DemoPlugin",
                    source_key="github:jxxghp/moviepilot-plugins",
                    source_type=TrustedPluginSourceType.OFFICIAL,
                    repo_url=REPO_URL,
                    package_generation="v3",
                    plugin_version=plugin_version,
                    dto={"v3": True},
                ),
            ),
            package_generation="v3",
        ),
    ))
